import discord
from discord.ext import commands
import asyncio
import os
from keep_alive import keep_alive
import pytchat
from datetime import datetime
import re
from rapidfuzz import fuzz
from collections import defaultdict, deque
import time

# إعداد البوت
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ✅ السماح فقط لمن لديهم الرتبة المحددة
ALLOWED_ROLE_ID = 1389955793520165046

# ✅ روم اللوجز (حط هنا ID الشانل اللي انت عاوزه للـ logs)
LOG_CHANNEL_ID = 1406224327912980480

@bot.check
async def global_check(ctx):
    if isinstance(ctx.author, discord.Member):
        allowed = any(role.id == ALLOWED_ROLE_ID for role in ctx.author.roles)
        if not allowed:
            await ctx.send("❌ ليس لديك الصلاحية لاستخدام هذا الأمر.")
        return allowed
    return False

# متغيرات التحكم
active_chats = {}
message_history = []  
user_last_messages = {}
user_message_times = defaultdict(deque)  # rate limit tracking

def normalize(text):
    text = re.sub(r'[^\w\s]', '', text)  
    text = re.sub(r'[ًٌٍَُِّْـ]', '', text)  
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

async def log_message(ctx, reason, author_name, content):
    """إرسال رسالة مرفوضة للـ logs channel"""
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(
        title=f"🚫 رسالة تم رفضها ({reason})",
        description=f"👤 **{author_name}**\n💬 {content[:300]}",
        color=0xff5555,
        timestamp=datetime.now()
    )
    embed.set_footer(text="📺 YouTube Chat Logger")
    await log_channel.send(embed=embed)

async def reconnect_youtube_chat_silent(chat_data, channel_id):
    try:
        old_chat = chat_data['chat']
        if not old_chat.is_alive():
            return False
        return True
    except Exception:
        return False

@bot.event
async def on_ready():
    print(f'✅ {bot.user} متصل بـ Discord!')
    print(f'🔗 البوت موجود في {len(bot.guilds)} سيرفر')
    print(f'🆔 Bot ID: {bot.user.id}')
    await bot.change_presence(activity=discord.Game(name="!commands"))

@bot.command(name='explain')
async def explain_command(ctx):
    await ctx.send("**# ازاي تجيب Video ID؟**\nخد اللينك من اللايف، هتلاقي الكود زي المثال 👇")
    await asyncio.sleep(3)
    await ctx.send("`!start MKYi1QrW2jg&t=1612s` → هنا `MKYi1QrW2jg` هو الـ ID")
    await asyncio.sleep(3)
    images = [
        {"url": "https://i.postimg.cc/RZg19WHQ/1.png", "description": "📌 مكان الاي دي من الكمبيوتر."},
        {"url": "https://i.postimg.cc/m2wCNP8f/2.png", "description": "📌 خطوات من الموبايل: 1."},
        {"url": "https://i.postimg.cc/sf5px6W2/3.png", "description": "2."},
        {"url": "https://i.postimg.cc/VL1XCq9W/4.png", "description": "3"}
    ]
    for item in images:
        embed = discord.Embed(description=item["description"], color=0x00aaff)
        embed.set_image(url=item["url"])
        await ctx.send(embed=embed)
        await asyncio.sleep(3)

@bot.command(name='start')
async def start_youtube_chat(ctx, video_id: str = None):
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("❌ هذا الأمر لا يعمل في الخاص!")
        return
    if not video_id:
        await ctx.send("❌ يرجى إدخال كود الفيديو أو اللينك")
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
            await ctx.send("❌ الفيديو موجود بس البث مش مباشر حالياً!")
            return

        active_chats[channel_id] = {'chat': chat, 'running': True, 'video_id': video_id}
        embed = discord.Embed(
            title="✅ تم الاتصال بنجاح!",
            description=f"بدأ نقل رسائل البث",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        embed.add_field(name="📺 Video ID", value=video_id, inline=True)
        embed.add_field(name="📍 روم Discord", value=ctx.channel.mention, inline=True)
        embed.set_footer(text="© 2025 Ahmed Magdy")
        await ctx.send(embed=embed)

        bot.loop.create_task(monitor_youtube_chat(ctx, channel_id))

    except Exception as e:
        await ctx.send(f'❌ خطأ:\n```{str(e)}```')

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

                # Rate limit: 5 رسائل / 10 ثواني
                now = time.time()
                times = user_message_times[author_name]
                times.append(now)
                while times and now - times[0] > 10:
                    times.popleft()
                if len(times) > 5:
                    await log_message(ctx, "Rate Limit", author_name, message_content)
                    continue

                # Anti-spam similarity
                user_msgs = user_last_messages.get(author_name, [])
                if any(fuzz.ratio(normalized_current, normalize(m)) > 92 for m in user_msgs):
                    await log_message(ctx, "Similar Spam", author_name, message_content)
                    continue
                if any(fuzz.ratio(normalized_current, m) > 92 for m in message_history[-10:]):
                    await log_message(ctx, "Duplicate Global", author_name, message_content)
                    continue

                # Update history
                user_msgs.append(message_content)
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

                msg_display = message_content[:800] + "..." if len(message_content) > 800 else message_content or "*رسالة فارغة أو ايموجي*"

                embed = discord.Embed(
                    title="🎬 **YouTube Live Chat**",
                    description=f"### 👤 **{c.author.name}**\n\n### 💬 {fix_mixed_text(msg_display)}",
                    color=0xff0000,
                    timestamp=timestamp
                )
                if hasattr(c.author, 'imageUrl') and c.author.imageUrl:
                    embed.set_thumbnail(url=c.author.imageUrl)
                message_count += 1
                embed.set_footer(
                    text=f"📺 YouTube Live Chat • رسالة #{message_count}",
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
        await ctx.send('⚠️ لا يوجد شات YouTube نشط')
        return
    active_chats[channel_id]['running'] = False
    del active_chats[channel_id]
    embed = discord.Embed(
        title="⏹️ تم إيقاف YouTube Chat",
        description="تم إيقاف نقل الرسائل",
        color=0xffa500
    )
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
    await ctx.send(embed=embed)

@bot.command(name='commands')
async def commands_help(ctx):
    embed = discord.Embed(
        title="🎬 YouTube Live Chat Bot - المساعدة",
        description="بوت تنظيم رسايل اللايف بفلترة قوية + لوجز",
        color=0x0099ff
    )
    commands_text = """
    `!start VIDEO_ID_or_LINK` - بدء نقل رسائل
    `!stop` - إيقاف النقل
    `!status` - حالة البوت
    `!explain` - شرح جلب الـ ID
    `!commands` - قائمة الأوامر
    """
    embed.add_field(name="📋 الأوامر المتاحة", value=commands_text, inline=False)
    embed.add_field(name="💡 ملاحظات", 
                   value="• البوت يمنع السبام والرسائل المتكررة\n"
                        "• بيعمل Rate limit 5 رسائل / 10 ثواني للشخص\n"
                        "• أي رسالة مرفوضة بتروح للـ Logs Channel", 
                   inline=False)
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
