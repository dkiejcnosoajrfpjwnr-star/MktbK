import asyncio
import json
import os
import logging
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from pyrogram import Client
from pyrogram.errors import (
    PeerIdInvalid, ChannelInvalid, UsernameInvalid,
    UsernameNotOccupied, FloodWait,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==============================================================
#  الاعدادات - كلها من متغيرات البيئة (Secrets)
# ==============================================================
BOT_TOKEN    = os.environ["BOT_TOKEN"]
OWNER_ID     = int(os.environ["OWNER_ID"])
API_ID       = int(os.environ["API_ID"])
API_HASH     = os.environ["API_HASH"]
SESSION_STR  = os.environ["SESSION_STR"]
RELAY_CHAT_ID = int(os.environ.get("RELAY_CHAT_ID", "0"))  # معرف مجموعة الريلاي

# القنوات مفصولة بفاصلة: @ch1,https://t.me/ch2,ch3
_ENV_CHANNELS = os.environ.get("CHANNELS", "")

START_MESSAGE = (
    "🌟 مرحبًا بك في بوت مكتبة الكتب\n\n"
    "📚 مكتبة رقمية مجانية تضم أكثر من مليون كتاب\n"
    "🔎 يمكنك البحث بسهولة بكتابة اسم الكتاب أو جزء منه\n\n"
    "🧭 تعليمات البحث الصحيحة:\n"
    "✔️ اكتب اسم الكتاب فقط\n"
    "✔️ أو جزء واضح من العنوان\n\n"
    "❌ أمثلة بحث غير صحيحة:\n"
    "✖️ كلمات عشوائية\n"
    "✖️ جمل طويلة أو أوصاف\n\n"
    "⚖️ تنويه قانوني:\n"
    "إدارة وفريق بوت مكتبة الكتب يحترمون حقوق الملكية الفكرية احترامًا تامًا.\n"
    "جميع الملفات المفهرسة تم رفعها من قبل مستخدمي تيليجرام أو قنوات عامة.\n"
    "في حال وجود أي محتوى مخالف لحقوق النشر, يرجى التواصل معنا وسيتم حذفه فورًا.\n\n"
    "📩 باستخدامك للبوت فأنت تقرّ بذلك.\n\n"
    "📖 نتمنى لك قراءة ممتعة!"
)

DATA_FILE        = "data.json"
RESULTS_PER_PAGE = 10
RELAY_TIMEOUT    = 60
SEARCH_LIMIT     = 500

_data_cache: Optional[dict] = None


# -- مساعدة: تحليل رابط/اسم قناة ---------------------------------
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
    if not _ENV_CHANNELS.strip():
        return []
    result = []
    for part in _ENV_CHANNELS.split(","):
        ch = normalize_channel(part)
        if ch and ch not in result:
            result.append(ch)
    return result


# -- البيانات -------------------------------------------------------
def load_data() -> dict:
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            _data_cache = json.load(f)
            return _data_cache
    _data_cache = {
        "channels": [],
        "channel_ids": {},
    }
    return _data_cache


def save_data(data: dict) -> None:
    global _data_cache
    _data_cache = data
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_pdf(msg) -> bool:
    if not msg.document:
        return False
    doc = msg.document
    if doc.mime_type and doc.mime_type == "application/pdf":
        return True
    if doc.file_name and doc.file_name.lower().endswith(".pdf"):
        return True
    return False


# -- Pyrogram -------------------------------------------------------
pyro: Optional[Client] = None




async def resolve_channel(username: str) -> Optional[int]:
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
        logger.info(f"Resolved {username} -> {chat.id}")
        return chat.id
    except asyncio.TimeoutError:
        logger.warning(f"Timeout resolving {username}")
    except (PeerIdInvalid, ChannelInvalid, UsernameInvalid):
        logger.warning(f"Cannot resolve {username}")
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s resolving {username}")
    except Exception as e:
        logger.warning(f"Error resolving {username}: {e}")
    return None


async def start_pyro(app: Application) -> None:
    global pyro
    pyro = Client(
        "book_session",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STR,
    )
    await pyro.start()
    logger.info("Pyrogram connected - loading dialogs cache...")

    try:
        count = 0
        async def _load():
            nonlocal count
            async for _ in pyro.get_dialogs():
                count += 1
        await asyncio.wait_for(_load(), timeout=30)
        logger.info(f"Loaded {count} dialogs into cache")
    except asyncio.TimeoutError:
        logger.warning("Dialogs load timed out")
    except Exception as e:
        logger.warning(f"Could not load dialogs: {e}")

    # تحميل مجموعة الريلاي من السكريت في الكاش
    try:
        chat = await asyncio.wait_for(pyro.get_chat(RELAY_CHAT_ID), timeout=15)
        logger.info(f"Relay group loaded: {chat.title} ({RELAY_CHAT_ID})")
    except Exception as e:
        logger.warning(f"Could not load relay group: {e}")

    # دمج قنوات البيئة + الانضمام إليها
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
            logger.info(f"Added {added} channels from CHANNELS env var")

    # حل معرفات جميع القنوات فقط (بدون انضمام)
    data = load_data()
    for ch in data.get("channels", []):
        await resolve_channel(ch)
        await asyncio.sleep(0.3)


async def stop_pyro(app: Application) -> None:
    global pyro
    if pyro and pyro.is_connected:
        await pyro.stop()


# -- البحث ---------------------------------------------------------
async def _search_single_channel(ch: str, query: str, ids: dict) -> list:
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
    except (PeerIdInvalid, ChannelInvalid, UsernameInvalid, UsernameNotOccupied):
        logger.warning(f"Channel not found or inaccessible, removing: {ch}")
        data = load_data()
        data.get("channel_ids", {}).pop(ch, None)
        save_data(data)
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s in {ch} - skipping")
    except Exception as e:
        logger.error(f"Search error ({ch}): {e}")
    return results


async def search_books(query: str, channels: list) -> list:
    if not pyro or not pyro.is_connected:
        return []
    data = load_data()
    ids  = data.get("channel_ids", {})
    channel_results = await asyncio.gather(
        *[_search_single_channel(ch, query, ids) for ch in channels],
        return_exceptions=False,
    )
    return [r for ch_results in channel_results for r in ch_results]


# -- الريلاي --------------------------------------------------------
async def _do_relay(chat: str, msg_id: int, user_id: int, bot) -> None:
    relay_msg_id = None
    try:
        try:
            await asyncio.wait_for(pyro.get_chat(chat), timeout=15)
        except Exception:
            pass
        relay_msg = await pyro.copy_message(
            chat_id=RELAY_CHAT_ID,
            from_chat_id=chat,
            message_id=msg_id,
            caption="",
        )
        relay_msg_id = relay_msg.id
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=RELAY_CHAT_ID,
            message_id=relay_msg_id,
            caption="",
        )
    finally:
        if relay_msg_id:
            try:
                await pyro.delete_messages(RELAY_CHAT_ID, relay_msg_id)
            except Exception:
                pass


async def deliver_via_relay(user_id: int, chat: str, msg_id: int, bot) -> tuple[bool, str]:
    if not RELAY_CHAT_ID:
        return False, "مجموعة الريلاي غير مضبوطة في السكريت."
    if not pyro or not pyro.is_connected:
        return False, "عميل Pyrogram غير متصل."
    try:
        await asyncio.wait_for(
            _do_relay(chat, msg_id, user_id, bot),
            timeout=RELAY_TIMEOUT,
        )
        return True, ""
    except asyncio.TimeoutError:
        return False, f"انتهت مهلة الإرسال ({RELAY_TIMEOUT}s). حاول مجدداً."
    except Exception as e:
        logger.error(f"Relay error [{chat}/{msg_id}]: {e}")
        return False, str(e)


# -- لوحات المفاتيح ------------------------------------------------
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def results_keyboard(results: list, page: int) -> InlineKeyboardMarkup:
    start = page * RESULTS_PER_PAGE
    end   = start + RESULTS_PER_PAGE
    buttons = []
    for abs_idx, r in enumerate(results[start:end], start=start):
        buttons.append([InlineKeyboardButton(r["name"][:64], callback_data=f"sb:{abs_idx}", style="success")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("السابق", callback_data=f"pg:{page - 1}", style="danger"))
    if end < len(results):
        nav.append(InlineKeyboardButton("التالي", callback_data=f"pg:{page + 1}", style="primary"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# -- الاوامر --------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_MESSAGE)


async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text.strip()
    if not query:
        return
    data = load_data()
    if not data["channels"]:
        await update.message.reply_text("لا توجد قنوات مكتبية مضافة بعد.")
        return

    msg     = await update.message.reply_text(f"جاري البحث عن: {query}...")
    results = await search_books(query, data["channels"])

    # آخر بحث فقط لكل مستخدم
    ctx.user_data["results"] = results
    ctx.user_data["query"]   = query

    if not results:
        await msg.edit_text(f"لم يتم العثور على نتائج لـ: {query}")
        return

    text = f"نتائج البحث عن: {query}\nعدد النتائج: {len(results)}\n\nاضغط على الكتاب لاستلامه:"
    await msg.edit_text(text, reply_markup=results_keyboard(results, 0))


async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    await q.answer()
    page    = int(q.data.split(":")[1])
    results = ctx.user_data.get("results")
    query   = ctx.user_data.get("query", "")
    if not results:
        await q.message.edit_text("انتهت الجلسة. ابحث مجدداً.")
        return
    text = f"نتائج البحث عن: {query}\nعدد النتائج: {len(results)}\n\nاضغط على الكتاب لاستلامه:"
    await q.message.edit_text(text, reply_markup=results_keyboard(results, page))


async def cb_send_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("جاري الإرسال...")
    idx     = int(q.data.split(":")[1])
    results = ctx.user_data.get("results", [])
    if not results or idx >= len(results):
        await q.message.reply_text("انتهت الجلسة. ابحث مجدداً.")
        return
    r       = results[idx]
    user_id = q.from_user.id
    success, err = await deliver_via_relay(user_id, r["chat"], r["msg_id"], ctx.bot)
    if not success:
        await q.message.reply_text(f"تعذّر إرسال الملف.\n\nالسبب: {err}")


# -- التشغيل --------------------------------------------------------
def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(start_pyro)
        .post_shutdown(stop_pyro)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_page,      pattern="^pg:"))
    app.add_handler(CallbackQueryHandler(cb_send_book, pattern="^sb:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
