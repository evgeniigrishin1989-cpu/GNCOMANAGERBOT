# main.py
import os
import re
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx  # для OpenAI вызовов
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ========= ENV & CONST =========
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

OPS_CHAT_ID = os.getenv("OPS_CHAT_ID")
OPS_CHAT_ID = int(OPS_CHAT_ID) if OPS_CHAT_ID else None

TZ = ZoneInfo(os.getenv("TZ", "Africa/Johannesburg"))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например https://gncomanagerbot-1.onrender.com
BOT_SECRET = os.getenv("BOT_SECRET", "gncohook")  # путь вебхука: /gncohook
PORT = int(os.getenv("PORT", "10000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

DB_PATH = "gnco_ro.sqlite"

# ========= DB =========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            tg_user_id INTEGER,
            tg_username TEXT,
            phone TEXT,
            make_model TEXT,
            plate TEXT,
            odometer INTEGER,
            issue TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def insert_ro(data: dict, user) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO ro (created_at, tg_user_id, tg_username, phone, make_model, plate, odometer, issue)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(TZ).isoformat(timespec="seconds"),
            user.id if user else None,
            getattr(user, "username", None),
            data.get("phone", ""),
            data.get("make_model", ""),
            data.get("plate", ""),
            int(data["odometer"]) if str(data.get("odometer", "")).isdigit() else None,
            data.get("issue", ""),
        ),
    )
    ro_id = c.lastrowid
    conn.commit()
    conn.close()
    return ro_id


# ========= Utils & Validation =========
PHONE_RE = re.compile(r"\+27\d{9}")  # ЮАР формат: +27XXXXXXXXX

def norm_plate(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(" ", "").replace("-", "")
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s[:10]

def safe_int(text: str) -> int | None:
    try:
        n = int(re.sub(r"[^\d]", "", text))
        return n if 0 < n < 10_000_000 else None
    except Exception:
        return None

def ro_preview(data: dict) -> str:
    return (
        f"📞 Телефон: {data.get('phone') or '—'}\n"
        f"🏍️ Байк: {data.get('make_model') or '—'}\n"
        f"🚘 Номер: {data.get('plate') or '—'}\n"
        f"🧭 Пробег: {data.get('odometer') or '—'} км\n"
        f"❗ Проблема: {data.get('issue') or '—'}"
    )

# ========= AI helpers =========
async def llm_chat(messages, temperature: float = 0.3) -> str | None:
    """Простой вызов OpenAI Chat Completions. Вернёт текст или None."""
    if not OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": OPENAI_MODEL, "temperature": temperature, "messages": messages},
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print("LLM error:", e)
        return None


async def ai_extract_ro(text: str) -> dict:
    """
    Пытаемся вытащить поля RO из произвольного текста.
    Всегда есть fallback на регексы; при наличии OPENAI_API_KEY просим модель вернуть JSON.
    """
    # Fallback по правилам
    phone = None
    m = PHONE_RE.search(text.replace(" ", ""))
    if m:
        phone = m.group(0)

    plate = None
    tokens = re.findall(r"[A-Za-z0-9\-]{4,10}", text)
    if tokens:
        plate = norm_plate(tokens[0])

    odometer = None
    nums = [int(x.replace(" ", "")) for x in re.findall(r"\d[\d\s]{2,}", text)]
    if nums:
        cand = max(nums)
        if 0 < cand < 10_000_000:
            odometer = cand

    fallback = {
        "phone": phone or "",
        "make_model": "",
        "plate": plate or "",
        "odometer": odometer if odometer is not None else "",
        "issue": text[:400],
    }

    if not OPENAI_API_KEY:
        return fallback

    sys = (
        "Ты помощник сервиса мототехники. Извлеки поля заявки из текста клиента. "
        "Верни ТОЛЬКО JSON с ключами: phone(+27...), make_model, plate(upper, без пробелов), "
        "odometer(целое км), issue(кратко)."
    )
    user = f"Текст клиента:\n{text}"
    out = await llm_chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    )
    if not out:
        return fallback
    try:
        out_clean = out.strip()
        out_clean = re.sub(r"^```json|```$", "", out_clean, flags=re.IGNORECASE | re.MULTILINE).strip()
        data = json.loads(out_clean)
        data["phone"] = data.get("phone") or fallback["phone"]
        data["make_model"] = (data.get("make_model") or "").strip()
        data["plate"] = norm_plate(data.get("plate") or fallback["plate"])
        odo = data.get("odometer")
        data["odometer"] = int(odo) if isinstance(odo, (int, str)) and str(odo).isdigit() else fallback["odometer"]
        data["issue"] = (data.get("issue") or fallback["issue"]).strip()[:400]
        return data
    except Exception as e:
        print("AI JSON parse error:", e, out)
        return fallback

# ========= Conversation states =========
PHONE, MAKE_MODEL, PLATE, ODOMETER, ISSUE, CONFIRM = range(6)

# ========= Handlers =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я «Менеджер» GNCO. Оформлю заявку (RO), подскажу цену и сроки.\n"
        "Быстрый старт: /ro — и погнали.\n"
        "Нужна болталка с ИИ — /ai <вопрос>."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/ro — открыть заявку\n"
        "/ai <вопрос> — спросить ИИ\n"
        "/id — показать chat_id\n"
        "/cancel — отменить заполнение",
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"chat_id: `{chat.id}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Окей, отменил. Если что — /ro.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---- /ro flow ----
async def ro_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ок, начнём. Введите номер телефона в формате +27XXXXXXXXX",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PHONE

async def ro_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").replace(" ", "")
    m = PHONE_RE.fullmatch(text)
    if not m:
        await update.message.reply_text("Формат телефона: +27XXXXXXXXX. Попробуй ещё раз.")
        return PHONE
    context.user_data["phone"] = m.group(0)
    await update.message.reply_text("Марка и модель байка (например, Honda XR150):")
    return MAKE_MODEL

async def ro_make_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["make_model"] = (update.message.text or "").strip()[:100]
    await update.message.reply_text("Госномер/plate (буквы и цифры):")
    return PLATE

async def ro_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plate"] = norm_plate(update.message.text or "")
    await update.message.reply_text("Пробег, км:")
    return ODOMETER

async def ro_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = safe_int(update.message.text or "")
    if n is None:
        await update.message.reply_text("Введи целое число (км).")
        return ODOMETER
    context.user_data["odometer"] = n
    await update.message.reply_text("Коротко опиши проблему:")
    return ISSUE

async def ro_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["issue"] = (update.message.text or "").strip()[:400]
    kb = ReplyKeyboardMarkup([["✅ Создать", "❌ Отмена"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Проверь, всё верно:\n" + ro_preview(context.user_data) + "\n\nЖмём?",
        reply_markup=kb,
    )
    return CONFIRM

async def ro_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").lower()
    if "отмена" in txt or "cancel" in txt or "❌" in txt:
        return await cmd_cancel(update, context)
    data = context.user_data.copy()
    ro_id = insert_ro(data, update.effective_user)

    card = (
        f"🆕 RO#{ro_id} {datetime.now(TZ).strftime('%d.%m.%Y, %H:%M (%Z)')}\n"
        f"Клиент: @{update.effective_user.username or '—'}\n"
        f"{ro_preview(data)}"
    )
    if OPS_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=OPS_CHAT_ID, text=card)
        except Exception as e:
            print("Send to OPS failed:", e)

    await update.message.reply_text(f"Готово! RO#{ro_id} открыт ✅", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# ---- AI chat ----
async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(maxsplit=1)
    tail = parts[1] if len(parts) > 1 else ""
    if not OPENAI_API_KEY:
        await update.message.reply_text("AI сейчас оффлайн (нет API ключа). Но я могу оформить заявку: /ro 😉")
        return
    if not tail:
        await update.message.reply_text("Спроси что-нибудь. Пример: /ai сколько стоит замена цепи?")
        return
    sys = (
        "Ты дружелюбный менеджер GNCO. Отвечай кратко, по делу. RU/EN. "
        "Если вопрос про ремонт — предложи /ro."
    )
    reply = await llm_chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": tail}],
        temperature=0.5,
    )
    await update.message.reply_text(reply or "Хмм… попробуй ещё раз 🙏")

# ---- Smart intake (свободный текст) ----
async def smart_intake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Подтверждение после превью
    if context.user_data.get("await_ai_confirm"):
        ans = text.lower().strip()
        if ans in ("да", "yes", "y", "ok", "ок", "ага"):
            data = context.user_data.get("ai_draft", {})
            ro_id = insert_ro(data, update.effective_user)
            card = (
                f"🆕 RO#{ro_id} {datetime.now(TZ).strftime('%d.%m.%Y, %H:%M (%Z)')}\n"
                f"Клиент: @{update.effective_user.username or '—'}\n"
                f"{ro_preview(data)}"
            )
            if OPS_CHAT_ID:
                await context.bot.send_message(chat_id=OPS_CHAT_ID, text=card)
            await update.message.reply_text(f"Готово! RO#{ro_id} открыт ✅")
        else:
            await update.message.reply_text("Окей, не создаём. Если нужно — /ro 😉")
        context.user_data.pop("await_ai_confirm", None)
        context.user_data.pop("ai_draft", None)
        return

    # Попытка вытащить поля RO из одного сообщения
    data = await ai_extract_ro(text)
    filled = sum(bool(data.get(k)) for k in ("phone", "make_model", "plate", "odometer"))
    if filled >= 3:
        preview = (
            "Понял так:\n"
            f"{ro_preview(data)}\n\n"
            "Создать заявку? Напиши **да** или **нет**."
        )
        context.user_data["await_ai_confirm"] = True
        context.user_data["ai_draft"] = data
        await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN)
    else:
        if OPENAI_API_KEY:
            sys = "Ты дружелюбный менеджер GNCO. Помоги человеку кратко и по делу."
            reply = await llm_chat(
                [{"role": "system", "content": sys}, {"role": "user", "content": text}]
            )
            await update.message.reply_text(reply or "Могу оформить заявку — набери /ro")
        else:
            await update.message.reply_text("Могу оформить заявку — набери /ro. Или спроси /help.")

# ========= main() =========
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /ro conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("ro", ro_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_phone)],
            MAKE_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_make_model)],
            PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_plate)],
            ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_odometer)],
            ISSUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_issue)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ro_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ai", cmd_ai))

    # smart intake (после conv!)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        smart_intake
    ))

    if not WEBHOOK_URL:
        # На всякий случай можно локально/без вебхуков: run_polling
        print("WEBHOOK_URL is not set -> running polling (dev mode)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    # Вебхук
    url_path = BOT_SECRET  # '/gncohook'
    full_webhook = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"
    print(f"Starting webhook on 0.0.0.0:{PORT}, path: /{url_path}")
    print(f"Setting Telegram webhook to: {full_webhook}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=full_webhook,
        allowed_updates=Update.ALL_TYPES,
        secret_token=None,  # можно задать, если хочешь проверять X-Telegram-Bot-Api-Secret-Token
    )

if __name__ == "__main__":
    main()
