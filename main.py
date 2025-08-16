import discord
from discord.ext import commands, tasks
import asyncio
import os
from keep_alive import keep_alive
import pytchat
from datetime import datetime
import re
from rapidfuzz import fuzz

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ✅ السماح فقط لمن لديهم الرتبة المحددة
ALLOWED_ROLE_ID = 1389955793520165046

@bot.check
async def global_check(ctx):
    if isinstance(ctx.author, discord.Member):
        allowed = any(role.id == ALLOWED_ROLE_ID for role in ctx.author.roles)
        if not allowed:
            await ctx.send("❌ ليس لديك الصلاحية لاستخدام هذا الأمر.")
        return allowed
    return False

# متغيرات للتحكم في الشات
active_chats = {}
message_history = []  # آخر الرسائل العامة
user_last_messages = {}

def normalize(text):
    text = re.sub(r'[^\w\s]', '', text)  # إزالة الرموز
    text = re.sub(r'[ًٌٍَُِّْـ]', '', text)  # إزالة التشكيل
    return text.strip().lower()

def fix_mixed_text(text):
    if re.search(r'[\u0600-\u06FF]', text) and re.search(r'[a-zA-Z]', text):
        return '\u202B' + text + '\u202C'
    return text

def extract_video_id(text):
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11})(?:[&?]|\s|$)',
        r'youtu\.be\/([0-9A-Za-z_-]{11})',
        r'studio\.youtube\.com\/video\/([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return text.strip()

async def reconnect_youtube_chat_silent(chat_data, channel_id):
    """
    إعادة اتصال صامتة تكمل من آخر مكان بدون رسائل مكررة.
    """
    try:
        old_chat = chat_data['chat']
        if not old_chat.is_alive():
            return False
        # نفس الـ object → هيحتفظ بالـ continuation
        return True
    except Exception:
        return False

@bot.event
async def on_ready():
    print(f'✅ {bot.user} متصل بـ Discord!')
    print(f'🔗 البوت موجود في {len(bot.guilds)} سيرفر')
    print(f'🆔 Bot ID: {bot.user.id}')
    await bot.change_presence(activity=discord.Game(name="!commands"))

@bot.command(name='start')
async def start_youtube_chat(ctx, video_id: str = None):
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("❌ هذا الأمر لا يعمل في الخاص!")
        return
    if not video_id:
        await ctx.send("❌ يرجى إدخال كود الفيديو\nمثال: `!start dQw4w9WgXcQ`")
        return

    video_id = extract_video_id(video_id)
    channel_id = ctx.channel.id
    if channel_id in active_chats:
        await ctx.send("⚠️ يوجد شات نشط بالفعل! استخدم `!stop` لإيقافه.")
        return

    await ctx.send(f'🔄 محاولة الاتصال بـ YouTube Live Chat...\n📺 Video ID: `{video_id}`')
    try:
        chat = pytchat.create(video_id=video_id)
        if not chat.is_alive():
            await ctx.send("❌ تم العثور على الفيديو لكن البث غير مباشر حاليًا!")
            return

        active_chats[channel_id] = {'chat': chat, 'running': True, 'video_id': video_id}
        embed = discord.Embed(title="✅ تم الاتصال بنجاح!", description=f"بدأ نقل رسائل البث", color=0x00ff00, timestamp=datetime.now())
        embed.add_field(name="📺 Video ID", value=video_id, inline=True)
        embed.add_field(name="📍 روم Discord", value=ctx.channel.mention, inline=True)
        embed.set_footer(text="© 2025 Ahmed Magdy")
        await ctx.send(embed=embed)

        bot.loop.create_task(monitor_youtube_chat(ctx, channel_id))

    except Exception as e:
        await ctx.send(f'❌ خطأ في الاتصال:\n```{str(e)}```')

async def monitor_youtube_chat(ctx, channel_id):
    global message_history, user_last_messages
    chat_data = active_chats.get(channel_id)
    if not chat_data:
        return

    chat = chat_data['chat']
    video_id = chat_data['video_id']
    message_count = 0
    reconnect_attempts = 0
    max_reconnects = 3
    ended_by_stream = False  

    try:
        while chat_data.get('running', False):
            loop = asyncio.get_event_loop()
            try:
                chat_data_result = await loop.run_in_executor(None, chat.get)
                items = chat_data_result.sync_items()
                reconnect_attempts = 0
            except Exception:
                try:
                    if not chat.is_alive():
                        ended_by_stream = True
                        break
                except:
                    pass

                reconnect_attempts += 1
                if reconnect_attempts > max_reconnects:
                    ended_by_stream = True
                    break

                success = await reconnect_youtube_chat_silent(chat_data, channel_id)
                if not success:
                    ended_by_stream = True
                    break
                continue

            if not items:
                await asyncio.sleep(5)
                try:
                    if not chat.is_alive():
                        ended_by_stream = True
                        break
                except:
                    pass
                continue

            for c in items:
                if not chat_data.get('running', False):
                    break

                message_content = c.message.strip() if c.message else ""
                author_name = c.author.name
                normalized_current = normalize(message_content)

                # نخزن كل رسائل الشخص مش آخر واحدة بس
                user_msgs = user_last_messages.get(author_name, [])
                
                # لو أي رسالة سابقة شبه الرسالة الحالية بنسبة > 85% → اعتبرها سبام
                if any(fuzz.ratio(normalized_current, normalize(m)) > 85 for m in user_msgs):
                    continue
                if any(fuzz.ratio(normalized_current, m) > 85 for m in message_history[-10:]):
                    continue
                
                # نضيف الرسالة الجديدة لقائمة الشخص
                user_msgs.append(message_content)
                
                # نخلي الحد الأقصى لكل شخص 100 رسالة بس
                if len(user_msgs) > 100:
                    user_msgs = user_msgs[-100:]
                
                user_last_messages[author_name] = user_msgs
                message_history.append(normalized_current)
                if len(message_history) > 50:
                    message_history = message_history[-50:]


                try:
                    timestamp = datetime.fromisoformat(c.datetime.replace('Z', '+00:00')) if c.datetime else datetime.now()
                except:
                    timestamp = datetime.now()

                message_content_display = message_content[:800] + "..." if len(message_content) > 800 else message_content or "*رسالة فارغة او ايموجي*"

                embed = discord.Embed(
                    title="🎬 **YouTube Live Chat**",
                    description=f"### 👤 **{c.author.name}**\n\n### 💬 {fix_mixed_text(message_content_display)}",
                    color=0xff0000,
                    timestamp=timestamp
                )
                if hasattr(c.author, 'imageUrl') and c.author.imageUrl:
                    embed.set_thumbnail(url=c.author.imageUrl)
                message_count += 1
                embed.set_footer(
                    text=f"📺 YouTube Live Chat • رسالة #{message_count} • 🔥",
                    icon_url="https://upload.wikimedia.org/wikipedia/commons/4/42/YouTube_icon_%282013-2017%29.png"
                )
                try:
                    await ctx.send(embed=embed)
                    await asyncio.sleep(0.5)
                except:
                    pass
            await asyncio.sleep(3)
    finally:
        if channel_id in active_chats:
            del active_chats[channel_id]
        if ended_by_stream:
            try:
                await ctx.send("# 📴 **تم إيقاف البوت تلقائيًا لأن البث انتهى.**")
            except:
                pass

@bot.command(name='stop')
async def stop_youtube_chat(ctx):
    channel_id = ctx.channel.id
    if channel_id not in active_chats:
        await ctx.send('⚠️ لا يوجد شات YouTube نشط في هذه القناة')
        return
    active_chats[channel_id]['running'] = False
    del active_chats[channel_id]
    embed = discord.Embed(
        title="⏹️ تم إيقاف YouTube Chat",
        description="تم إيقاف نقل الرسائل بنجاح",
        color=0xffa500
    )
    embed.set_footer(text="© 2025 Ahmed Magdy", icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
    await ctx.send(embed=embed)

@bot.command(name='status')
async def status(ctx):
    active_count = len(active_chats)
    embed = discord.Embed(
        title="📊 حالة البوت",
        color=0x00ff00 if active_count > 0 else 0x999999
    )
    embed.add_field(name="🔗 الاتصال", value="متصل ✅", inline=True)
    embed.add_field(name="📺 الشاتات النشطة", value=f"{active_count}", inline=True)
    embed.add_field(name="🏓 Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    if active_count > 0:
        channels = [f"<#{channel_id}>" for channel_id in active_chats.keys()]
        embed.add_field(name="📍 الرومات النشطة", value="\n".join(channels), inline=False)
    embed.set_footer(text="© 2025 Ahmed Magdy", icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
    await ctx.send(embed=embed)

@bot.command(name='commands')
async def commands_help(ctx):
    embed = discord.Embed(
        title="🎬 YouTube Live Chat Bot - المساعدة",
        description="بوت تنظيم رسايل اللايف بتقنية بسيطة وسلسة",
        color=0x0099ff
    )
    commands_text = """
    `!start VIDEO_ID_or_LINK` - بدء نقل رسائل من يوتيوب لايف
    `!stop` - إيقاف النقل فوراً
    `!status` - عرض تفاصيل حالة البوت
    `!explain` - شرح ازاي تجيب الاي دي
    `!commands` - عرض قائمة المساعدة الكاملة
    """
    embed.add_field(name="📋 الأوامر المتاحة", value=commands_text, inline=False)
    embed.add_field(name="💡 نصائح مهمة", 
                   value="• تأكد من أن الفيديو يحتوي على Live Chat نشط\n"
                        "• البوت يتجنب الرسائل المتكررة والسبام تلقائياً\n"
                        "• يمكن تشغيل شات واحد فقط لكل روم Discord\n"
                        "• البوت يدعم الرسائل العربية والإنجليزية\n"
                        "• 🌟 تحديث جديد : يمكنك الان استخدام لينك بدل من الاعتماد على الاي دي فقط 🌟", 
                   inline=False)
    embed.set_footer(text="© 2025 Ahmed Magdy - جميع الحقوق محفوظة", 
                    icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
    await ctx.send(embed=embed)
    
async def main():
    keep_alive()
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("❌ لم يتم العثور على التوكن")
        return
    try:
        print("🚀 بدء تشغيل البوت...")
        await bot.start(token)
    except Exception as e:
        print(f"❌ خطأ: {e}")

if __name__ == '__main__':
    asyncio.run(main())
