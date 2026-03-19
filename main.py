import discord
from discord.ext import commands
import asyncio
import os
import json
from keep_alive import keep_alive
import pytchat
from datetime import datetime
import re
from rapidfuzz import fuzz
from collections import defaultdict, deque
import time
from typing import List, Tuple, Set

# ============================================================
#                      إعداد البوت
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ✅ السماح فقط لمن لديهم الرتبة المحددة
ALLOWED_ROLE_ID = 1389955793520165046

# ✅ روم اللوجز (حط هنا ID الشانل اللي انت عاوزه للـ logs)
LOG_CHANNEL_ID = 1406224327912980480

# ============================================================
#                   حالة / تخزين داخلي
# ============================================================
active_chats = {}
user_last_messages = defaultdict(lambda: deque(maxlen=150))
user_message_times = defaultdict(deque)  # key: (guild_id, channel_id, author_name)
log_message_counts = defaultdict(int)  # NEW
user_message_numbers = defaultdict(dict)  # key: (guild_id, channel_id, author_name) -> dict: {message_content: message_number}

# ============================================================
#              تخزين الأشخاص اللي ظهروا كتير في اللوجز (مؤقت لكل بث)
# ============================================================
JUNKED_USERS_FILE = "junked_users.json"
JUNK_EXPIRE_SECONDS = 8 * 60 * 60  # 8 ساعات

# هيبقى كده: {video_id: {"users": {author_name: {count, name, image}}, "ended_at": timestamp_or_None}}
junked_users_data = {}

def load_junked_users():
    global junked_users_data
    if os.path.exists(JUNKED_USERS_FILE):
        try:
            with open(JUNKED_USERS_FILE, "r", encoding="utf-8") as f:
                junked_users_data = json.load(f)
        except Exception:
            junked_users_data = {}

def save_junked_users():
    try:
        with open(JUNKED_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(junked_users_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def clear_expired_junk():
    now = time.time()
    changed = False
    for vid, info in list(junked_users_data.items()):
        ended_at = info.get("ended_at")
        if ended_at and now - ended_at > JUNK_EXPIRE_SECONDS:
            junked_users_data[vid]["users"] = {}
            junked_users_data[vid]["ended_at"] = None
            changed = True
    if changed:
        save_junked_users()

def get_current_video_id(channel_id):
    chat_data = active_chats.get(channel_id)
    if chat_data and chat_data.get("video_id"):
        return chat_data["video_id"]
    return None

# ============================================================
#                   إعدادات الفلاتر/العَتبات
# ============================================================
RATE_LIMIT_MAX_MSG = 5
RATE_LIMIT_WINDOW_SEC = 10
THRESHOLD_TOKEN_SORT = 92
THRESHOLD_TOKEN_SET  = 92
THRESHOLD_JACCARD    = 0.90

# ============================================================
#                   أدوات التطبيع (Normalization)
# ============================================================
_AR_DIACRITICS_PATTERN = re.compile(r'[\u064B-\u065F\u0610-\u061A\u06D6-\u06ED]')
_TATWEEL_PATTERN       = re.compile(r'[\u0640]')
_CONTROL_CHARS_PATTERN = re.compile(
    r'[\u200B-\u200F\u061C\u202A-\u202E\u2066-\u2069]'
)
_PUNCT_NUM_PATTERN     = re.compile(r'[^\w\s]')
_MULTI_SPACE           = re.compile(r'\s+')

def _arabic_unify_letters(text: str) -> str:
    text = re.sub(r'[إأٱآا]', 'ا', text)
    text = re.sub(r'[يى]', 'ي', text)
    text = re.sub(r'[ة]', 'ه', text)
    text = re.sub(r'[ؤئ]', 'ء', text)
    return text

def _normalize_repeated_letters(text: str) -> str:
    return re.sub(r'(.)\1{2,}', r'\1\1', text)

def normalize(text: str) -> str:
    if not text:
        return ''
    text = _CONTROL_CHARS_PATTERN.sub('', text)
    text = _AR_DIACRITICS_PATTERN.sub('', text)
    text = _TATWEEL_PATTERN.sub('', text)
    text = _arabic_unify_letters(text)
    text = text.lower()
    text = _PUNCT_NUM_PATTERN.sub(' ', text)
    text = _normalize_repeated_letters(text)
    text = _MULTI_SPACE.sub(' ', text).strip()
    return text

def tokens_sorted(text: str) -> List[str]:
    if not text:
        return []
    toks = text.split()
    toks.sort()
    return toks

def jaccard_similarity(a_tokens: List[str], b_tokens: List[str]) -> float:
    if not a_tokens and not b_tokens:
        return 1.0
    A, B = set(a_tokens), set(b_tokens)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    inter = len(A.intersection(B))
    union = len(A.union(B))
    return inter / union if union else 0.0

def strong_semantic_similarity(a: str, b: str) -> Tuple[bool, dict]:
    na, nb = normalize(a), normalize(b)
    if not na and not nb:
        return True, {'reason': 'empty_after_normalize'}
    tsort = fuzz.token_sort_ratio(na, nb)
    tset  = fuzz.token_set_ratio(na, nb)
    ja = tokens_sorted(na)
    jb = tokens_sorted(nb)
    jacc = jaccard_similarity(ja, jb)
    similar = (tsort >= THRESHOLD_TOKEN_SORT) or (tset >= THRESHOLD_TOKEN_SET) or (jacc >= THRESHOLD_JACCARD)
    info = {
        'token_sort_ratio': tsort,
        'token_set_ratio': tset,
        'jaccard': round(jacc, 3),
        'na': na,
        'nb': nb
    }
    return similar, info

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

# ============================================================
#                     دالة اللوجز المعدلة
# ============================================================
def log_junk_user(video_id, author_name, author_image):
    if video_id not in junked_users_data:
        junked_users_data[video_id] = {"users": {}, "ended_at": None}
    u = junked_users_data[video_id]["users"].setdefault(author_name, {
        "count": 0, "author_name": author_name, "author_image": author_image
    })
    u["count"] += 1
    u["author_name"] = author_name
    if author_image:
        u["author_image"] = author_image
    save_junked_users()

async def log_message(ctx, reason, author_name, content, extra: dict = None, author_image=None):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    channel_id = ctx.channel.id if ctx and hasattr(ctx, 'channel') else LOG_CHANNEL_ID

    video_id = get_current_video_id(channel_id)
    if video_id:
        log_junk_user(video_id, author_name, author_image)

    log_message_counts[channel_id] += 1
    log_count = log_message_counts[channel_id]
    desc = f"👤 **{author_name}**\n"
    if content:
        desc += f"💬 {content[:600]}"
    embed = discord.Embed(
        title=f"🚫 رسالة تم رفضها ({reason})",
        description=desc,
        color=0xff5555,
        timestamp=datetime.now()
    )
    if extra:
        details = []
        if 'token_sort_ratio' in extra: details.append(f"token_sort: {extra['token_sort_ratio']}")
        if 'token_set_ratio'  in extra: details.append(f"token_set: {extra['token_set_ratio']}")
        if 'jaccard'          in extra: details.append(f"jaccard: {extra['jaccard']}")
        if details:
            embed.add_field(name="Similarity", value=", ".join(str(x) for x in details), inline=False)
        if 'similar_message_number' in extra and extra['similar_message_number']:
            link = f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}/{extra['similar_message_id']}"
            embed.add_field(
                name="🔁 مكررة من رسالة رقم",
                value=f"[الرسالة الأصلية كانت رقم #{extra['similar_message_number']}]({link})",
                inline=False
            )
    embed.set_footer(text=f"📺 YouTube Chat Logger • رسالة #{log_count}")
    try:
        await log_channel.send(embed=embed)
    except:
        pass

async def reconnect_youtube_chat_silent(chat_data, channel_id):
    try:
        old_chat = chat_data['chat']
        if not old_chat.is_alive():
            return False
        return True
    except Exception:
        return False

@bot.check
async def global_check(ctx):
    if isinstance(ctx.author, discord.Member):
        allowed = any(role.id == ALLOWED_ROLE_ID for role in ctx.author.roles)
        if not allowed:
            await ctx.send("❌ ليس لديك الصلاحية لاستخدام هذا الأمر.")
        return allowed
    return False

@bot.event
async def on_ready():
    load_junked_users()
    clear_expired_junk()
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

    for d in (user_last_messages, user_message_numbers, user_message_times):
        keys_to_remove = [k for k in d.keys() if k[1] == channel_id]
        for k in keys_to_remove:
            del d[k]
    log_message_counts[channel_id] = 0

    # عند بدء بث جديد نصفر قائمة junk لهذا البث
    junked_users_data[video_id] = {"users": {}, "ended_at": None}
    save_junked_users()

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
        embed.set_footer(text="© 2026 Ahmed Magdy")
        await ctx.send(embed=embed)

        bot.loop.create_task(monitor_youtube_chat(ctx, channel_id))

    except Exception as e:
        await ctx.send(f'❌ خطأ:\n```{str(e)}```')

# ============================================================
#                 قلب الفلترة داخل المراقبة
# ============================================================
async def monitor_youtube_chat(ctx, channel_id):
    chat_data = active_chats.get(channel_id)
    if not chat_data:
        return

    chat = chat_data['chat']
    video_id = chat_data['video_id']
    message_count = 0
    reconnect_attempts = 0
    max_reconnects = 3
    ended_by_stream = False

    PROBE_ATTEMPTS = 4
    PROBE_SLEEP_SEC = 5
    RECREATE_ATTEMPTS = 3
    RECREATE_SLEEP_SEC = 5

    last_message_time = time.time()
    MAX_NO_MESSAGE_SECONDS = 1320

    try:
        while chat_data.get('running', False):
            loop = asyncio.get_event_loop()
            items = None
            try:
                chat_data_result = await loop.run_in_executor(None, chat.get)
                items = chat_data_result.sync_items()
                reconnect_attempts = 0
            except Exception:
                probe_found = False
                for _ in range(PROBE_ATTEMPTS):
                    await asyncio.sleep(PROBE_SLEEP_SEC)
                    try:
                        probe = await loop.run_in_executor(None, chat.get)
                        probe_items = probe.sync_items()
                        if probe_items:
                            items = probe_items
                            probe_found = True
                            break
                    except:
                        continue
                if not probe_found:
                    recreated = False
                    for _ in range(RECREATE_ATTEMPTS):
                        try:
                            new_chat = pytchat.create(video_id=video_id)
                            if new_chat and new_chat.is_alive():
                                chat = new_chat
                                chat_data['chat'] = new_chat
                                recreated = True
                                break
                        except:
                            pass
                        await asyncio.sleep(RECREATE_SLEEP_SEC)
                    if not recreated and not probe_found:
                        ended_by_stream = True
                        break

            if not items:
                probe_found = False
                for _ in range(PROBE_ATTEMPTS):
                    await asyncio.sleep(PROBE_SLEEP_SEC)
                    try:
                        probe = await loop.run_in_executor(None, chat.get)
                        probe_items = probe.sync_items()
                        if probe_items:
                            items = probe_items
                            probe_found = True
                            break
                    except:
                        continue

                if not probe_found:
                    recreated = False
                    for _ in range(RECREATE_ATTEMPTS):
                        try:
                            new_chat = pytchat.create(video_id=video_id)
                            if new_chat and new_chat.is_alive():
                                chat = new_chat
                                chat_data['chat'] = new_chat
                                recreated = True
                                break
                        except:
                            pass
                        await asyncio.sleep(RECREATE_SLEEP_SEC)
                    if not recreated and not probe_found:
                        ended_by_stream = True
                        break

            if not items:
                await asyncio.sleep(1)
                if time.time() - last_message_time > MAX_NO_MESSAGE_SECONDS:
                    ended_by_stream = True
                    break
                continue

            for c in items:
                if not chat_data.get('running', False):
                    break
                message_content_raw = c.message if c.message else ""
                message_content = message_content_raw.strip()
                author_name = c.author.name
                key = (ctx.guild.id if ctx.guild else 0, ctx.channel.id, author_name)
                now = time.time()
                times = user_message_times[key]
                times.append(now)
                while times and now - times[0] > RATE_LIMIT_WINDOW_SEC:
                    times.popleft()
                if len(times) > RATE_LIMIT_MAX_MSG:
                    await log_message(
                        ctx, 
                        "Rate Limit", 
                        author_name, 
                        message_content,
                        author_image=getattr(c.author, 'imageUrl', None)
                    )
                    continue

                past_msgs: deque = user_last_messages[key]
                is_spam_similar = False
                debug_info = None
                similar_message_number = None

                compare_sample = list(past_msgs)[-80:] if len(past_msgs) > 80 else list(past_msgs)

                for prev in reversed(compare_sample):
                    similar, info = strong_semantic_similarity(message_content, prev)
                    if similar:
                        is_spam_similar = True
                        debug_info = info
                        similar_message_number = user_message_numbers[key].get(prev)
                        break

                if is_spam_similar:
                    await log_message(
                        ctx, 
                        "Similar Spam (Per-User)", 
                        author_name, 
                        message_content, 
                        {**(debug_info or {}), "similar_message_number": similar_message_number},
                        author_image=getattr(c.author, 'imageUrl', None)
                    )
                    continue

                past_msgs.append(message_content)
                message_count += 1
                user_message_numbers[key][message_content] = message_count

                try:
                    try:
                        timestamp = datetime.fromisoformat(c.datetime.replace('Z', '+00:00')) if c.datetime else datetime.now()
                    except:
                        timestamp = datetime.now()

                    msg_display = (
                        message_content[:800] + "..."
                        if len(message_content) > 800
                        else (message_content or "*رسالة فارغة أو ايموجي*")
                    )

                    embed = discord.Embed(
                        title="🎬 **YouTube Live Chat**",
                        description=f"### 👤 **{c.author.name}**\n\n### 💬 {fix_mixed_text(msg_display)}",
                        color=0xff0000,
                        timestamp=timestamp
                    )
                    if hasattr(c.author, 'imageUrl') and c.author.imageUrl:
                        embed.set_thumbnail(url=c.author.imageUrl)
                    embed.set_footer(
                        text=f"📺 YouTube Live Chat • رسالة #{message_count}",
                        icon_url="https://upload.wikimedia.org/wikipedia/commons/4/42/YouTube_icon_%282013-2017%29.png"
                    )
                    await ctx.send(embed=embed)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                last_message_time = time.time()

            await asyncio.sleep(0.5)
            if time.time() - last_message_time > MAX_NO_MESSAGE_SECONDS:
                ended_by_stream = True
                break

    finally:
        if channel_id in active_chats:
            del active_chats[channel_id]
        for d in (user_last_messages, user_message_numbers, user_message_times):
            keys_to_remove = [k for k in d.keys() if k[1] == channel_id]
            for k in keys_to_remove:
                del d[k]
        log_message_counts[channel_id] = 0
        # عند انتهاء البث سجل وقت الانتهاء
        video_id = chat_data["video_id"] if chat_data else None
        if video_id and video_id in junked_users_data:
            junked_users_data[video_id]["ended_at"] = time.time()
            save_junked_users()

            # ========= إرسال قائمة الـ junk تلقائيًا قبل رسالة الإيقاف =========
            threshold = 30
            users = junked_users_data[video_id]["users"]
            junked_users = [u for u in users.values() if u["count"] >= threshold]
            if junked_users:
                for u in junked_users:
                    embed = discord.Embed(
                        title="🚫 شخص صنف Junk",
                        description=f"**{u['author_name']}**\nظهر في اللوجز: {u['count']} مرة",
                        color=0xff5555
                    )
                    if u["author_image"]:
                        embed.set_thumbnail(url=u["author_image"])
                    await ctx.send(embed=embed)
            else:
                await ctx.send("✅ لا يوجد أشخاص صنفوا كـ junk في هذا البث.")

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
    for d in (user_last_messages, user_message_numbers, user_message_times):
        keys_to_remove = [k for k in d.keys() if k[1] == channel_id]
        for k in keys_to_remove:
            del d[k]
    log_message_counts[channel_id] = 0

    embed = discord.Embed(
        title="⏹️ تم إيقاف YouTube Chat",
        description="تم إيقاف نقل الرسائل",
        color=0xffa500
    )
    embed.set_footer(text="© 2026 Ahmed Magdy", icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
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
    embed.set_footer(text="© 2026 Ahmed Magdy", icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
    await ctx.send(embed=embed)

@bot.command(name='change_name')
async def change_name(ctx, *, new_name: str = None):
    if not new_name:
        await ctx.send("❌ يجب إرسال الاسم الجديد بعد الأمر.\nمثال: `!change_name Ahmed`")
        return
    try:
        await bot.user.edit(username=new_name)
        await ctx.send(f"✅ تم تغيير اسم البوت إلى: **{new_name}**")
    except Exception as e:
        await ctx.send(f"❌ حدث خطأ أثناء تغيير الاسم:\n```{str(e)}```")

@bot.command(name='change_photo')
async def change_photo(ctx):
    await ctx.send("📸 أرسل صورة البوت الآن (كـ ملف أو صورة) خلال 30 ثانية.")
    def check(m):
        return m.author == ctx.author and m.attachments and m.channel == ctx.channel

    try:
        msg = await bot.wait_for('message', timeout=30.0, check=check)
        attachment = msg.attachments[0]
        img_bytes = await attachment.read()
        await bot.user.edit(avatar=img_bytes)
        await ctx.send("✅ تم تغيير صورة البوت بنجاح!")
    except asyncio.TimeoutError:
        await ctx.send("⏰ انتهى الوقت! أعد إرسال الأمر.")
    except Exception as e:
        await ctx.send(f"❌ حدث خطأ:\n```{str(e)}```")

@bot.command(name='change_banner')
async def change_banner(ctx):
    await ctx.send("🖼️ أرسل صورة البانر الآن (كـ ملف أو صورة) خلال 30 ثانية..")

    def img_check(m):
        return m.author == ctx.author and m.attachments and m.channel == ctx.channel

    def desc_check(m):
        return m.author == ctx.author and not m.attachments and m.channel == ctx.channel

    try:
        img_msg = await bot.wait_for('message', timeout=30.0, check=img_check)
        banner_bytes = await img_msg.attachments[0].read()
        desc_msg = None
        try:
            desc_msg = await bot.wait_for('message', timeout=30.0, check=desc_check)
            banner_desc = desc_msg.content
        except asyncio.TimeoutError:
            banner_desc = None

        await bot.user.edit(banner=banner_bytes)
        await ctx.send("✅ تم تغيير بانر البوت بنجاح!")
        if banner_desc:
            await ctx.send(f"📋 وصف البانر الجديد:\n{banner_desc}")

    except asyncio.TimeoutError:
        await ctx.send("⏰ انتهى الوقت! أعد إرسال الأمر.")
    except Exception as e:
        await ctx.send(f"❌ حدث خطأ:\n```{str(e)}```")

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
    `!junk` - عرض قائمة الأشخاص المخربين للبث الحالي
    `!junk_clear` - تصفير قائمة الأشخاص المخربين للبث الحالي
    """
    embed.add_field(name="📋 الأوامر المتاحة", value=commands_text, inline=False)
    commands_appearance = """
    `!change_name` - تغيير اسم البوت
    `!change_photo` - تغيير صورة البوت
    `!change_banner` - تغيير بانر البوت
    """
    embed.add_field(name="🤖 اوامر مظهر البوت", value=commands_appearance, inline=False)
    embed.add_field(name="💡 نصائح مهمة", 
                   value="• تأكد من أن الفيديو يحتوي على Live Chat نشط\n"
                        "• البوت يتجنب الرسائل المتكررة والسبام تلقائياً\n"
                        "• يمكن تشغيل شات واحد فقط لكل روم Discord\n"
                        "• البوت يدعم الرسائل العربية والإنجليزية\n"
                        "• 🌟 تحديث جديد : يمكنك الان استخدام لينك بدل من الاعتماد على الاي دي فقط 🌟", 
                   inline=False)
    embed.set_footer(text="© 2026 Ahmed Magdy - جميع الحقوق محفوظة", 
                    icon_url="https://cdn.discordapp.com/emojis/741243683501817978.png")
    await ctx.send(embed=embed)

# ============================================================
#             أوامر junk و junk_clear (مؤقت للبث الحالي)
# ============================================================
@bot.command(name='junk')
async def junk_command(ctx):
    clear_expired_junk()
    video_id = get_current_video_id(ctx.channel.id)
    if not video_id or video_id not in junked_users_data:
        await ctx.send("لا يوجد بث حالياً لكي يعرض قائمة المخربين.")
        return
    threshold = 30
    users = junked_users_data[video_id]["users"]
    junked_users = [u for u in users.values() if u["count"] >= threshold]
    if not junked_users:
        await ctx.send("✅ لا يوجد أشخاص صنفوا كـ junk حتى الآن.")
        return
    for u in junked_users:
        embed = discord.Embed(
            title="🚫 شخص صنف Junk",
            description=f"**{u['author_name']}**\nظهر في اللوجز: {u['count']} مرة",
            color=0xff5555
        )
        if u["author_image"]:
            embed.set_thumbnail(url=u["author_image"])
        await ctx.send(embed=embed)

@bot.command(name='junk_clear')
async def junk_clear_command(ctx):
    video_id = get_current_video_id(ctx.channel.id)
    if not video_id or video_id not in junked_users_data:
        await ctx.send("لا يوجد قائمة لهذا البث.")
        return
    junked_users_data[video_id]["users"] = {}
    junked_users_data[video_id]["ended_at"] = None
    save_junked_users()
    await ctx.send("✅ تم تصفير قائمة الـ junk لهذا البث بنجاح.")

# ============================================================
#                     Main Loop
# ============================================================
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
