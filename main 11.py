import asyncio
import json
import os
import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from pyrogram import Client
from pyrogram.errors import (
    PeerIdInvalid, ChannelInvalid, UsernameInvalid,
    FloodWait, UserAlreadyParticipant, UserNotParticipant,
    InviteHashExpired, ChatAdminRequired,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
#  إعدادات من متغيرات البيئة (Secrets)
# ══════════════════════════════════════════════════════════
BOT_TOKEN   = os.environ["BOT_TOKEN"]          # توكن البوت
OWNER_ID    = int(os.environ["OWNER_ID"])      # معرف المالك
API_ID      = int(os.environ["API_ID"])        # Telegram API ID
API_HASH    = os.environ["API_HASH"]           # Telegram API Hash
SESSION_STR = os.environ["SESSION_STR"]        # Pyrogram session string

# القنوات من متغير البيئة CHANNELS (مفصولة بفاصلة)
# مثال: @channel1,https://t.me/channel2,channel3
_env_channels_raw = os.environ.get("CHANNELS", "")

DATA_FILE        = "data.json"
RESULTS_PER_PAGE = 10
RELAY_TIMEOUT    = 60
SEARCH_LIMIT     = 500   # عدد الرسائل المبحوثة لكل قناة

# كاش البيانات في الذاكرة
_data_cache: Optional[dict] = None

# حالات المحادثة
AWAIT_START_MSG, AWAIT_CHANNEL_LINK, AWAIT_RELAY_ID = range(3)


# ── مساعدة: تحليل رابط/اسم قناة ───────────────────────────
def normalize_channel(text: str) -> str:
    text = text.strip()
    if text.startswith("https://t.me/"):
        text = "@" + text.replace("https://t.me/", "").split("/")[0].rstrip("/")
    elif text.startswith("t.me/"):
        text = "@" + text.replace("t.me/", "").split("/")[0].rstrip("/")
    elif not text.startswith("@"):
        text = "@" + text
    return text


def parse_env_channels() -> list:
    """يحوّل CHANNELS البيئي إلى قائمة يوزرنيمات موحدة الصياغة."""
    if not _env_channels_raw.strip():
        return []
    result = []
    for part in _env_channels_raw.split(","):
        ch = normalize_channel(part)
        if ch and ch not in result:
            result.append(ch)
    return result


# ── البيانات ──────────────────────────────────────────────
def load_data() -> dict:
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            _data_cache = json.load(f)
            return _data_cache
    _data_cache = {
        "start_message": (
            "📚 مرحباً بك في بوت المكتبة!\n\n"
            "ابحث عن أي كتاب وسأجده لك من بين القنوات المكتبية."
        ),
        "channels": [],
        "channel_ids": {},
        "relay_chat_id": None,
    }
    return _data_cache


def save_data(data: dict) -> None:
    global _data_cache
    _data_cache = data
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_relay_id() -> Optional[int]:
    return load_data().get("relay_chat_id")


def is_pdf(msg) -> bool:
    if not msg.document:
        return False
    doc = msg.document
    if doc.mime_type and doc.mime_type == "application/pdf":
        return True
    if doc.file_name and doc.file_name.lower().endswith(".pdf"):
        return True
    return False


# ── Pyrogram ──────────────────────────────────────────────
pyro: Optional[Client] = None


async def join_channel(username: str) -> str:
    """
    يحاول تسجيل الحساب في القناة إذا لم يكن منضماً.
    يعيد نص وصفي لحالة الانضمام.
    """
    if not pyro or not pyro.is_connected:
        return "⚠️ Pyrogram غير متصل."
    try:
        await asyncio.wait_for(pyro.join_chat(username), timeout=20)
        logger.info(f"✅ انضم الحساب إلى {username}")
        return f"✅ انضم الحساب إلى {username}"
    except UserAlreadyParticipant:
        logger.info(f"ℹ️ الحساب منضم مسبقاً إلى {username}")
        return f"ℹ️ الحساب منضم مسبقاً لـ {username}"
    except (ChannelInvalid, UsernameInvalid, PeerIdInvalid):
        logger.warning(f"⚠️ القناة {username} غير متاحة أو غير موجودة")
        return f"⚠️ القناة {username} غير موجودة أو غير متاحة"
    except InviteHashExpired:
        return f"⚠️ رابط الدعوة منتهي الصلاحية: {username}"
    except ChatAdminRequired:
        return f"⚠️ لا تملك صلاحية الانضمام لـ {username}"
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s عند محاولة الانضمام لـ {username}")
        await asyncio.sleep(e.value)
        return f"⏳ تم الانتظار وقد يكون الانضمام لـ {username} فشل — أعد المحاولة"
    except Exception as e:
        logger.warning(f"⚠️ خطأ عند الانضمام لـ {username}: {e}")
        return f"⚠️ خطأ عند الانضمام لـ {username}: {e}"


async def start_pyro(app: Application) -> None:
    global pyro
    pyro = Client(
        "book_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STR,
    )
    await pyro.start()
    logger.info("✅ Pyrogram متصل — جاري بناء كاش الـ peers...")

    # تحميل الـ dialogs لبناء كاش الـ peers
    try:
        count = 0
        async def _load_dialogs():
            nonlocal count
            async for _ in pyro.get_dialogs():
                count += 1
        await asyncio.wait_for(_load_dialogs(), timeout=30)
        logger.info(f"✅ تم تحميل {count} محادثة في الكاش")
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ انتهت مهلة تحميل الـ dialogs (تم تحميل {count} حتى الآن)")
    except Exception as e:
        logger.warning(f"⚠️ تعذّر تحميل الـ dialogs: {e}")

    # دمج القنوات من متغير البيئة مع data.json
    env_channels = parse_env_channels()
    if env_channels:
        data = load_data()
        added = 0
        for ch in env_channels:
            if ch not in data["channels"]:
                data["channels"].append(ch)
                added += 1
        if added:
            save_data(data)
            logger.info(f"✅ تمت إضافة {added} قناة من متغير البيئة CHANNELS")

    # تحميل الريلاي في الكاش
    relay_id = get_relay_id()
    if relay_id:
        try:
            chat = await asyncio.wait_for(pyro.get_chat(relay_id), timeout=15)
            logger.info(f"✅ مجموعة الريلاي: {chat.title} ({relay_id})")
        except Exception as e:
            logger.warning(f"⚠️ تعذّر تحميل الريلاي: {e}")

    # حل معرفات القنوات والانضمام إليها
    await resolve_and_join_all_channels()


async def stop_pyro(app: Application) -> None:
    global pyro
    if pyro and pyro.is_connected:
        await pyro.stop()


async def resolve_channel(username: str) -> Optional[int]:
    """يحوّل اليوزرنيم إلى معرف رقمي ويخزنه."""
    if not pyro or not pyro.is_connected:
        return None

    data = load_data()
    ids  = data.setdefault("channel_ids", {})

    if username in ids:
        return ids[username]

    try:
        chat = await asyncio.wait_for(pyro.get_chat(username), timeout=20)
        ids[username] = chat.id
        save_data(data)
        logger.info(f"✅ حُلّ {username} → {chat.id}")
        return chat.id
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ انتهت مهلة حل {username}")
    except (PeerIdInvalid, ChannelInvalid, UsernameInvalid):
        logger.warning(f"⚠️ {username} غير متاح")
    except FloodWait as e:
        logger.warning(f"⚠️ FloodWait {e.value}s عند حل {username}")
    except Exception as e:
        logger.warning(f"⚠️ خطأ عند حل {username}: {e}")
    return None


async def resolve_and_join_all_channels() -> None:
    """يحل جميع القنوات وينضم إليها عند التشغيل."""
    data = load_data()
    for ch in data.get("channels", []):
        await join_channel(ch)
        await asyncio.sleep(0.5)
        await resolve_channel(ch)
        await asyncio.sleep(0.3)


async def _search_single_channel(ch: str, query: str, ids: dict) -> list:
    """يبحث في قناة واحدة بـ500 رسالة ويعيد ملفات PDF."""
    peer = ids.get(ch) or ch
    results = []
    try:
        async for msg in pyro.search_messages(peer, query=query, limit=SEARCH_LIMIT):
            if not is_pdf(msg):
                continue
            name = (msg.document.file_name or "").strip()
            if not name and msg.caption:
                name = msg.caption.split("\n")[0].strip()
            if not name:
                name = "كتاب PDF"
            results.append({
                "name":   name[:80],
                "chat":   ch,
                "msg_id": msg.id,
            })
    except (PeerIdInvalid, ChannelInvalid, UsernameInvalid):
        logger.warning(f"لا يمكن الوصول إلى: {ch}")
        data = load_data()
        ids_ref = data.get("channel_ids", {})
        if ch in ids_ref:
            del ids_ref[ch]
            save_data(data)
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s في {ch} — تخطي")
    except Exception as e:
        logger.error(f"خطأ في البحث ({ch}): {e}")
    return results


async def search_books(query: str, channels: list) -> list:
    """يبحث في كل القنوات بالتوازي ويعيد ملفات PDF فقط."""
    if not pyro or not pyro.is_connected:
        return []

    data = load_data()
    ids  = data.get("channel_ids", {})

    channel_results = await asyncio.gather(
        *[_search_single_channel(ch, query, ids) for ch in channels],
        return_exceptions=False,
    )

    results = [r for ch_results in channel_results for r in ch_results]
    return results


# ── الريلاي ───────────────────────────────────────────────
async def _do_relay(pyro_relay_id: int, relay_id: int, chat: str, msg_id: int, user_id: int, bot) -> None:
    relay_msg_id = None
    try:
        try:
            await asyncio.wait_for(pyro.get_chat(chat), timeout=15)
        except Exception:
            pass

        relay_msg = await pyro.copy_message(
            chat_id=pyro_relay_id,
            from_chat_id=chat,
            message_id=msg_id,
            caption="",
        )
        relay_msg_id = relay_msg.id
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=relay_id,
            message_id=relay_msg_id,
            caption="",
        )
    finally:
        if relay_msg_id:
            try:
                await pyro.delete_messages(relay_id, relay_msg_id)
            except Exception:
                pass


async def deliver_via_relay(user_id: int, chat: str, msg_id: int, bot) -> tuple[bool, str]:
    relay_id = get_relay_id()
    if not relay_id:
        return False, "مجموعة الريلاي غير مضبوطة."
    if not pyro or not pyro.is_connected:
        return False, "عميل Pyrogram غير متصل."

    try:
        relay_chat    = await asyncio.wait_for(pyro.get_chat(relay_id), timeout=15)
        pyro_relay_id = relay_chat.id
        await asyncio.wait_for(
            _do_relay(pyro_relay_id, relay_id, chat, msg_id, user_id, bot),
            timeout=RELAY_TIMEOUT,
        )
        return True, ""
    except asyncio.TimeoutError:
        logger.error(f"انتهت مهلة الريلاي [{chat}/{msg_id}]")
        return False, f"انتهت مهلة الإرسال ({RELAY_TIMEOUT}s). حاول مجدداً."
    except Exception as e:
        logger.error(f"خطأ في الريلاي [{chat}/{msg_id}]: {e}")
        return False, str(e)


# ── لوحات المفاتيح ────────────────────────────────────────
def main_keyboard(is_owner: bool = False) -> Optional[InlineKeyboardMarkup]:
    if not is_owner:
        return None
    relay_id  = get_relay_id()
    relay_btn = f"✅ الريلاي: {relay_id}" if relay_id else "⚙️ ضبط مجموعة الريلاي"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تعديل كليشة الستارت",   callback_data="edit_start")],
        [InlineKeyboardButton("📚 إدارة القنوات المكتبية", callback_data="manage_channels")],
        [InlineKeyboardButton(relay_btn,                   callback_data="set_relay")],
    ])


def channels_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة قناة",  callback_data="add_channel")],
        [InlineKeyboardButton("🗑️ حذف قناة",    callback_data="delete_channel")],
        [InlineKeyboardButton("📋 سجل القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("🔙 رجوع",        callback_data="back_main")],
    ])


def delete_channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(f"🗑 {ch}", callback_data=f"del:{ch}")] for ch in channels]
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="manage_channels")])
    return InlineKeyboardMarkup(buttons)


def results_keyboard(results: list, page: int) -> InlineKeyboardMarkup:
    start = page * RESULTS_PER_PAGE
    end   = start + RESULTS_PER_PAGE
    buttons = []
    for abs_idx, r in enumerate(results[start:end], start=start):
        label = f"{r['name']}"[:64]
        buttons.append([InlineKeyboardButton(label, callback_data=f"sb:{abs_idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"pg:{page - 1}"))
    if end < len(results):
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"pg:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# ── /start ────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data     = load_data()
    is_owner = update.effective_user.id == OWNER_ID
    await update.message.reply_text(data["start_message"], reply_markup=main_keyboard(is_owner))


# ── تعديل كليشة الستارت ───────────────────────────────────
async def cb_edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    data = load_data()
    await q.message.reply_text(
        f"الكليشة الحالية:\n\n{data['start_message']}\n\n"
        "أرسل النص الجديد أو /cancel للإلغاء:"
    )
    return AWAIT_START_MSG


async def received_start_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    data = load_data()
    data["start_message"] = update.message.text
    save_data(data)
    await update.message.reply_text("✅ تم تحديث كليشة الستارت!")
    return ConversationHandler.END


# ── ضبط الريلاي ───────────────────────────────────────────
async def cb_set_relay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    relay_id = get_relay_id()
    current  = f"الريلاي الحالي: `{relay_id}`\n\n" if relay_id else ""
    await q.message.reply_text(
        f"{current}"
        "📌 *كيفية ضبط مجموعة الريلاي:*\n\n"
        "1. أنشئ مجموعة خاصة جديدة\n"
        "2. أضف البوت والحساب الشخصي (Pyrogram) إلى المجموعة\n"
        "3. أضف @userinfobot للمجموعة وأرسل /start للحصول على ID\n"
        "4. أرسل الـ ID هنا (رقم يبدأ بـ -):\n\n"
        "مثال: `-1001234567890`\n\n"
        "أو /cancel للإلغاء",
        parse_mode="Markdown",
    )
    return AWAIT_RELAY_ID


async def received_relay_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        relay_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ المعرف غير صحيح. يجب أن يكون رقماً مثل:\n`-1001234567890`\n\nحاول مجدداً أو /cancel",
            parse_mode="Markdown",
        )
        return AWAIT_RELAY_ID

    try:
        chat = await ctx.bot.get_chat(relay_id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ البوت لا يستطيع الوصول للمجموعة.\n"
            f"تأكد أن البوت مضاف إليها.\n\nالخطأ: `{e}`\n\nحاول مجدداً أو /cancel",
            parse_mode="Markdown",
        )
        return AWAIT_RELAY_ID

    data = load_data()
    data["relay_chat_id"] = relay_id
    save_data(data)

    if pyro and pyro.is_connected:
        try:
            await asyncio.wait_for(pyro.get_chat(relay_id), timeout=15)
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ تم ضبط مجموعة الريلاي!\n"
        f"المجموعة: *{chat.title}*\n"
        f"ID: `{relay_id}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ── إدارة القنوات ─────────────────────────────────────────
async def cb_manage_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.message.edit_text("📚 إدارة القنوات المكتبية:", reply_markup=channels_menu_keyboard())


async def cb_add_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "أرسل يوزرنيم أو رابط القناة:\n"
        "مثال: @mybookchannel أو https://t.me/mybookchannel\n\n"
        "أو /cancel للإلغاء"
    )
    return AWAIT_CHANNEL_LINK


async def received_channel_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    username = normalize_channel(update.message.text)

    data = load_data()
    if username in data["channels"]:
        await update.message.reply_text("⚠️ هذه القناة مضافة مسبقاً!")
        return ConversationHandler.END

    data["channels"].append(username)
    save_data(data)
    await update.message.reply_text(f"✅ تمت إضافة القناة {username}\n🔄 جاري الانضمام وحل المعرف...")

    # الانضمام للقناة تلقائياً
    join_status = await join_channel(username)
    # حل المعرف الرقمي
    resolved_id = await resolve_channel(username)

    if resolved_id:
        await update.message.reply_text(
            f"{join_status}\n✅ تم ربط القناة (ID: `{resolved_id}`)",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"{join_status}\n⚠️ لم يتم حل معرف القناة. تأكد أن الحساب منضم للقناة."
        )
    return ConversationHandler.END


async def cb_delete_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = load_data()
    if not data["channels"]:
        await q.message.edit_text("⚠️ لا توجد قنوات مضافة.", reply_markup=channels_menu_keyboard())
        return
    await q.message.edit_text("اختر القناة للحذف:", reply_markup=delete_channels_keyboard(data["channels"]))


async def cb_del_channel_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q  = update.callback_query
    await q.answer()
    ch = q.data.split(":", 1)[1]
    data = load_data()
    if ch in data["channels"]:
        data["channels"].remove(ch)
        data.get("channel_ids", {}).pop(ch, None)
        save_data(data)
        await q.message.edit_text(f"✅ تم حذف {ch}!", reply_markup=channels_menu_keyboard())
    else:
        await q.message.edit_text("❌ القناة غير موجودة.", reply_markup=channels_menu_keyboard())


async def cb_list_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = load_data()
    ids  = data.get("channel_ids", {})
    if not data["channels"]:
        text = "📋 لا توجد قنوات مضافة."
    else:
        lines = "\n".join(
            f"{i+1}. {ch} {'✅' if ch in ids else '⚠️'}"
            for i, ch in enumerate(data["channels"])
        )
        text = (
            f"📋 القنوات المكتبية ({len(data['channels'])}):\n\n{lines}\n\n"
            "✅ = معرف محلول  ⚠️ = لم يُحلّ بعد"
        )
    await q.message.edit_text(text, reply_markup=channels_menu_keyboard())


async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data     = load_data()
    is_owner = q.from_user.id == OWNER_ID
    await q.message.edit_text(data["start_message"], reply_markup=main_keyboard(is_owner))


# ── البحث ─────────────────────────────────────────────────
# نخزن آخر نتائج بحث لكل مستخدم فقط (بدون تاريخ متعدد)
async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    if not query:
        return
    data = load_data()
    if not data["channels"]:
        await update.message.reply_text("⚠️ لا توجد قنوات مكتبية مضافة بعد.")
        return

    msg     = await update.message.reply_text(f"🔍 جاري البحث عن: {query}...")
    results = await search_books(query, data["channels"])

    # نحتفظ بآخر نتيجة بحث فقط (لا تاريخ متعدد)
    ctx.user_data["results"] = results
    ctx.user_data["query"]   = query

    if not results:
        await msg.edit_text(f"❌ لم يتم العثور على نتائج لـ: {query}")
        return

    text = f"📚 نتائج البحث عن: *{query}*\nعدد النتائج: {len(results)}\n\nاضغط على الكتاب لاستلامه:"
    await msg.edit_text(text, reply_markup=results_keyboard(results, 0), parse_mode="Markdown")


async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    results = ctx.user_data.get("results")
    query   = ctx.user_data.get("query", "")
    if not results:
        await q.message.edit_text("⚠️ انتهت الجلسة. ابحث مجدداً.")
        return
    text = f"📚 نتائج البحث عن: *{query}*\nعدد النتائج: {len(results)}\n\nاضغط على الكتاب لاستلامه:"
    await q.message.edit_text(text, reply_markup=results_keyboard(results, page), parse_mode="Markdown")


# ── إرسال الكتاب ──────────────────────────────────────────
async def cb_send_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("⏳ جاري الإرسال...")
    idx     = int(q.data.split(":")[1])
    results = ctx.user_data.get("results", [])
    if not results or idx >= len(results):
        await q.message.reply_text("⚠️ انتهت الجلسة. ابحث مجدداً.")
        return
    r       = results[idx]
    user_id = q.from_user.id
    success, err = await deliver_via_relay(user_id, r["chat"], r["msg_id"], ctx.bot)
    if not success:
        if not get_relay_id():
            await q.message.reply_text("⚙️ مجموعة الريلاي غير مضبوطة.\nالمالك يحتاج لضبطها.")
        else:
            await q.message.reply_text(f"❌ تعذّر إرسال الملف.\n\nالسبب: `{err}`", parse_mode="Markdown")


# ── إلغاء ─────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ تم الإلغاء.")
    return ConversationHandler.END


# ── التشغيل ───────────────────────────────────────────────
def main() -> None:
    owner_filter = filters.User(OWNER_ID)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_pyro)
        .post_shutdown(stop_pyro)
        .build()
    )

    edit_start_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_edit_start, pattern="^edit_start$")],
        states={AWAIT_START_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter, received_start_msg)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cb_back_main, pattern="^back_main$")],
        per_message=False, allow_reentry=True,
    )

    add_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_add_channel, pattern="^add_channel$")],
        states={AWAIT_CHANNEL_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter, received_channel_link)]},
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cb_manage_channels, pattern="^manage_channels$"),
            CallbackQueryHandler(cb_back_main,       pattern="^back_main$"),
            CallbackQueryHandler(cb_delete_channel,  pattern="^delete_channel$"),
            CallbackQueryHandler(cb_list_channels,   pattern="^list_channels$"),
        ],
        per_message=False, allow_reentry=True,
    )

    set_relay_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_set_relay, pattern="^set_relay$")],
        states={AWAIT_RELAY_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND & owner_filter, received_relay_id)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cb_back_main, pattern="^back_main$")],
        per_message=False, allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(edit_start_conv)
    app.add_handler(add_channel_conv)
    app.add_handler(set_relay_conv)
    app.add_handler(CallbackQueryHandler(cb_manage_channels,     pattern="^manage_channels$"))
    app.add_handler(CallbackQueryHandler(cb_delete_channel,      pattern="^delete_channel$"))
    app.add_handler(CallbackQueryHandler(cb_del_channel_confirm, pattern="^del:"))
    app.add_handler(CallbackQueryHandler(cb_list_channels,       pattern="^list_channels$"))
    app.add_handler(CallbackQueryHandler(cb_back_main,           pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cb_page,                pattern="^pg:"))
    app.add_handler(CallbackQueryHandler(cb_send_book,           pattern="^sb:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("🚀 البوت يعمل...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
