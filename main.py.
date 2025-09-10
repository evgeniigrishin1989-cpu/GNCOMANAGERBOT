import asyncio, os, re, sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, filters

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPS_CHAT_ID = os.getenv("OPS_CHAT_ID")
TZ = ZoneInfo(os.getenv("TZ", "Africa/Johannesburg"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://<имя>.onrender.com
BOT_SECRET = os.getenv("BOT_SECRET", "gncohook")
PORT = int(os.getenv("PORT", "10000"))
if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN not set")
OPS_CHAT_ID = int(OPS_CHAT_ID) if OPS_CHAT_ID else None

DB_PATH = "gnco_ro.sqlite"
def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS ro(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, tg_user_id INTEGER, tg_username TEXT,
        phone TEXT, make_model TEXT, plate TEXT, odometer INTEGER, issue TEXT);""")
    conn.commit(); conn.close()
def insert_ro(data, user):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("""INSERT INTO ro(ts,tg_user_id,tg_username,phone,make_model,plate,odometer,issue)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (datetime.now(TZ).strftime("%d.%m.%Y, %H:%М (%Z)"), user.id,
               f"@{user.username}" if user.username else "", data["phone"], data["make_model"],
               data["plate"], data["odometer"], data["issue"]))
    conn.commit(); rid = c.lastrowid; conn.close(); return rid

PHONE, MAKE_MODEL, PLATE, ODOMETER, ISSUE = range(5)
PHONE_RE = re.compile(r"^\+27\d{9}$"); PLATE_CLEAN = re.compile(r"[^A-Z0-9]")
def norm_plate(s): return PLATE_CLEAN.sub("", s.upper())

async def cmd_start(u:Update, c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Привет! Я менеджер GNCO 😊\nЧтобы начать — набери /ro\n\n"
        "Кинь: телефон (+27…), марку/модель, номер (без пробелов), пробег и что происходит. Можно фото/видео.")
async def cmd_help(u:Update, c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Команды: /ro /cancel /id /help")
async def cmd_id(u:Update, c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"chat_id: `{u.effective_chat.id}`", parse_mode=ParseMode.MARKDOWN)
async def cmd_ro(u:Update, c:ContextTypes.DEFAULT_TYPE):
    c.user_data.clear()
    await u.message.reply_text("Номер телефона в формате +27XXXXXXXXX",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True, one_time_keyboard=True))
    return PHONE
async def cancel(u:Update, c:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Окей, отменили. Когда будете готовы — /ro", reply_markup=ReplyKeyboardRemove()); c.user_data.clear()
    return ConversationHandler.END
async def ask_phone(u:Update, c:ContextTypes.DEFAULT_TYPE):
    t=u.message.text.strip()
    if t.lower()=="отмена": return await cancel(u,c)
    if not PHONE_RE.match(t):
        await u.message.reply_text("Нужен формат +27XXXXXXXXX. Ещё раз или «Отмена»."); return PHONE
    c.user_data["phone"]=t; await u.message.reply_text("Марка и модель? (напр. Honda CG 125)"); return MAKE_MODEL
async def ask_make_model(u:Update, c:ContextTypes.DEFAULT_TYPE):
    t=u.message.text.strip()
    if t.lower()=="отмена": return await cancel(u,c)
    c.user_data["make_model"]=t[:100]; await u.message.reply_text("Номер (я сделаю БЕЗ ПРОБЕЛОВ и заглавными):"); return PLATE
async def ask_plate(u:Update, c:ContextTypes.DEFAULT_TYPE):
    p=norm_plate(u.message.text)
    if not p or len(p)<3: await u.message.reply_text("Пришлите ещё раз (без пробелов), напр. CA12345."); return PLATE
    c.user_data["plate"]=p; await u.message.reply_text("Пробег (км), целым числом:"); return ODOMETER
async def ask_odometer(u:Update, c:ContextTypes.DEFAULT_TYPE):
    try: odo=int(u.message.text.replace(" ","")); assert odo>=0
    except: await u.message.reply_text("Нужно число км, например 45210."); return ODOMETER
    c.user_data["odometer"]=odo; await u.message.reply_text("Кратко: что происходит с байком?"); return ISSUE
async def finalize_issue(u:Update, c:ContextTypes.DEFAULT_TYPE):
    c.user_data["issue"]=u.message.text.strip()[:500]; rid=insert_ro(c.user_data, u.effective_user)
    card=(f"🆕 RO#{rid} {datetime.now(TZ).strftime('%d.%m.%Y, %H:%M (%Z)')}\nКлиент: @{u.effective_user.username or '—'}\n"
          f"Тел: {c.user_data['phone']}\nБайк: {c.user_data['make_model']}\nНомер: {c.user_data['plate']}\n"
          f"Пробег: {c.user_data['odometer']} км\nПроблема: {c.user_data['issue']}")
    if OPS_CHAT_ID: await c.bot.send_message(chat_id=OPS_CHAT_ID, text=card)
    await u.message.reply_text(f"Готово! RO#{rid} открыт ✅\nЕсли есть фото/видео — пришли сюда.", reply_markup=ReplyKeyboardRemove())
    c.user_data.clear(); return ConversationHandler.END
async def on_error(upd, ctx): print("Error:", ctx.error)

async def main():
    init_db()
    app=ApplicationBuilder().token(BOT_TOKEN).build()
    conv=ConversationHandler(
        entry_points=[CommandHandler("ro", cmd_ro)],
        states={PHONE:[MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
                MAKE_MODEL:[MessageHandler(filters.TEXT & ~filters.COMMAND, ask_make_model)],
                PLATE:[MessageHandler(filters.TEXT & ~filters.COMMAND, ask_plate)],
                ODOMETER:[MessageHandler(filters.TEXT & ~filters.COMMAND, ask_odometer)],
                ISSUE:[MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_issue)]},
        fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(conv); app.add_error_handler(on_error)

    WEBHOOK_URL=os.getenv("WEBHOOK_URL"); BOT_SECRET=os.getenv("BOT_SECRET","gncohook"); PORT=int(os.getenv("PORT","10000"))
    if not WEBHOOK_URL: raise RuntimeError("Set WEBHOOK_URL (https://<your-app>.onrender.com)")
    path=f"/{BOT_SECRET}"; url=f"{WEBHOOK_URL}{path}"
    await app.bot.set_webhook(url=url, allowed_updates=Update.ALL_TYPES)
    await app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=url)

if __name__=="__main__": asyncio.run(main())
