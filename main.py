import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
OPS_CHAT_ID = os.getenv("OPS_CHAT_ID")
TZ          = ZoneInfo(os.getenv("TZ", "Africa/Johannesburg"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # e.g. https://gncomanagerbot-1.onrender.com
BOT_SECRET  = os.getenv("BOT_SECRET", "gncohook")
PORT        = int(os.getenv("PORT", "10000"))   # Render/Railway set this automatically

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in Environment")

OPS_CHAT_ID = int(OPS_CHAT_ID) if OPS_CHAT_ID else None

# ---------- DB ----------
DB_PATH = "gnco_ro.sqlite"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS ro (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        tg_user_id INTEGER,
        tg_username TEXT,
        phone TEXT,
        make_model TEXT,
        plate TEXT,
        odometer INTEGER,
        issue TEXT
    );
    """)
    conn.commit()
    conn.close()

def insert_ro(data, user):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO ro (ts, tg_user_id, tg_username, phone, make_model, plate, odometer, issue)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(TZ).strftime("%d.%m.%Y, %H:%M (%Z)"),
        user.id,
        f"@{user.username}" if user.username else "",
        data["phone"],
        data["make_model"],
        data["plate"],
        data["odometer"],
        data["issue"]
    ))
    conn.commit()
    ro_id = c.lastrowid
    conn.close()
    return ro_id

# ---------- Conversation ----------
PHONE, MAKE_MODEL, PLATE, ODOMETER, ISSUE = range(5)
PHONE_RE    = re.compile(r"^\+27\d{9}$")
PLATE_CLEAN = re.compile(r"[^A-Z0-9]")

def norm_plate(s: str) -> str:
    return PLATE_CLEAN.sub("", s.upper())

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я менеджер GNCO 😊\n"
        "Чтобы открыть заявку — набери /ro\n\n"
        "Нужны: телефон (+27…), марка/модель, номер (без пробелов), пробег и что происходит.\n"
        "Можно сразу прислать фото/видео."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды: /ro /cancel /id /help")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"chat_id: `{update.effective_chat.id}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_ro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Ок, начнём. Введите номер телефона в формате +27XXXXXXXXX",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True, one_time_keyboard=True)
    )
    return PHONE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Окей, отменили. Когда будете готовы — /ro",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    if msg.lower() == "отмена":
        return await cancel(update, context)
    if not PHONE_RE.match(msg):
        await update.message.reply_text("Нужен формат +27XXXXXXXXX. Попробуйте ещё раз или нажмите «Отмена».")
        return PHONE
    context.user_data["phone"] = msg
    await update.message.reply_text("Марка и модель? (например: Honda CG 125)")
    return MAKE_MODEL

async def ask_make_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() == "отмена":
        return await cancel(update, context)
    context.user_data["make_model"] = txt[:100]
    await update.message.reply_text("Номерной знак (я сделаю БЕЗ ПРОБЕЛОВ и заглавными):")
    return PLATE

async def ask_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plate = norm_plate(update.message.text)
    if not plate or len(plate) < 3:
        await update.message.reply_text("Номер выглядит странно. Пришлите ещё раз (без пробелов), например CA12345.")
        return PLATE
    context.user_data["plate"] = plate
    await update.message.reply_text("Пробег (км), целым числом:")
    return ODOMETER

async def ask_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo = int(update.message.text.replace(" ", ""))
        if odo < 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Нужно число км, например 45210.")
        return ODOMETER
    context.user_data["odometer"] = odo
    await update.message.reply_text("Кратко: что происходит с байком?")
    return ISSUE

async def finalize_issue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    issue = update.message.text.strip()
    context.user_data["issue"] = issue[:500]
    ro_id = insert_ro(context.user_data, update.effective_user)

    card = (
        f"🆕 RO#{ro_id} {datetime.now(TZ).strftime('%d.%m.%Y, %H:%M (%Z)')}\n"
        f"Клиент: @{update.effective_user.username or '—'}\n"
        f"Телефон: {context.user_data['phone']}\n"
        f"Байк: {context.user_data['make_model']}\n"
        f"Номер: {context.user_data['plate']}\n"
        f"Пробег: {context.user_data['odometer']} км\n"
        f"Проблема: {context.user_data['issue']}"
    )
    if OPS_CHAT_ID:
        await context.bot.send_message(chat_id=OPS_CHAT_ID, text=card)

    await update.message.reply_text(
        f"Готово! RO#{ro_id} открыт ✅\n"
        f"Мы глянем байк и вернёмся с ценой. Если есть фото/видео — можно прислать сюда.",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Логи на Render видно в "All logs"
    print("Error:", context.error)

# ---------- ENTRY ----------
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("ro", cmd_ro)],
        states={
            PHONE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            MAKE_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_make_model)],
            PLATE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_plate)],
            ODOMETER:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_odometer)],
            ISSUE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_issue)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(conv)
    app.add_error_handler(on_error)

    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL is not set (пример: https://gncomanagerbot-1.onrender.com)")

    webhook_path = f"/{BOT_SECRET}"
    full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"

    print(f"Starting webhook on 0.0.0.0:{PORT}, path: {webhook_path}")
    print(f"Setting Telegram webhook to: {full_webhook_url}")

    # Синхронный запуск вебхука (PTB сам зарегистрирует webhook)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
    )

if __name__ == "__main__":
    main()
