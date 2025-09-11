import asyncio
import json
import logging
import os
import re
from typing import Optional

import httpx
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------
# ЛОГИ
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("gnco")

# -----------------------------
# ENV
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_SECRET = os.getenv("BOT_SECRET", "gncohook")  # будет частью URL и секретом вебхука
PUBLIC_BASE_URL = os.getenv("WEBHOOK_URL")         # https://<render>.onrender.com

ROAPP_API_KEY = os.getenv("ROAPP_API_KEY")            # если не задан, просто логируем
ROAPP_BASE_URL = os.getenv("ROAPP_BASE_URL", "https://api.roapp.io")
ROAPP_LOCATION_ID = os.getenv("ROAPP_LOCATION_ID")    # optional
ROAPP_SOURCE = os.getenv("ROAPP_SOURCE", "Telegram")

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not PUBLIC_BASE_URL:
    raise RuntimeError("WEBHOOK_URL is not set (e.g. https://<service>.onrender.com)")

# -----------------------------
# Вспомогалки
# -----------------------------
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

def normalize_phone(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    return f"+{digits}"

def tg_display_name(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "Telegram User"
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (u.username or f"id{u.id}")

# -----------------------------
# RO App client
# -----------------------------
class ROAppClient:
    def __init__(self, api_key: str, base_url: str = "https://api.roapp.io"):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )

    async def close(self):
        await self._client.aclose()

    async def create_inquiry(
        self,
        contact_phone: str,
        contact_name: str,
        title: str,
        description: str = "",
        location_id: Optional[int] = None,
        channel: Optional[str] = None,
    ) -> dict:
        payload = {
            "contact_phone": contact_phone,
            "contact_name": contact_name,
            "title": title or "Incoming request",
        }
        if description:
            payload["description"] = description
        if location_id:
            payload["location_id"] = int(location_id)
        if channel:
            payload["channel"] = channel

        r = await self._client.post("/lead/", json=payload)
        r.raise_for_status()
        return r.json()

RO = ROAppClient(ROAPP_API_KEY, ROAPP_BASE_URL) if ROAPP_API_KEY else None

# -----------------------------
# Telegram handlers
# -----------------------------
WELCOME = (
    "Ок, начнём. Пришлите номер телефона в формате +27XXXXXXXXXX — заведу заявку в CRM."
)
ASK_PHONE_AGAIN = "Пожалуйста, пришлите номер телефона в формате +XXXXXXXXXXX."

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # ждём телефон
    phone = normalize_phone(text)
    if not phone or not PHONE_RE.match(phone):
        context.user_data["last_msg"] = text  # на всякий
        await update.message.reply_text(ASK_PHONE_AGAIN)
        return

    name = tg_display_name(update)
    last_msg = context.user_data.get("last_msg") or ""

    if RO is None:
        await update.message.reply_text(
            "Телефон получил ✅\nRO App API ключ не настроен — заявку в CRM не создаю."
        )
        return

    try:
        inquiry = await RO.create_inquiry(
            contact_phone=phone,
            contact_name=name,
            title="Запрос на ремонт байка",
            description=f"Источник: Telegram.\nПоследнее сообщение: {last_msg}".strip(),
            location_id=int(ROAPP_LOCATION_ID) if ROAPP_LOCATION_ID else None,
            channel=ROAPP_SOURCE,
        )
        context.user_data["phone"] = phone
        context.user_data["inquiry_id"] = inquiry.get("id")
        await update.message.reply_text(
            "Готово! ✅ Заявка создана в CRM.\n"
            f"Номер: <b>{phone}</b>\nИмя: <b>{name}</b>",
            parse_mode=ParseMode.HTML,
        )
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(
            "❌ Не удалось создать заявку в CRM.\n"
            f"HTTP {e.response.status_code}\n{e.response.text[:600]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка интеграции: {e}")

# любое текстовое сообщение
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        context.user_data["last_msg"] = update.message.text.strip()
    await handle_text(update, context)

# -----------------------------
# AIOHTTP web app (Telegram + CRM + healthz)
# -----------------------------
def make_aiohttp_app(ptb_app: Application) -> web.Application:
    app = web.Application()

    # 1) Telegram webhook endpoint: /<BOT_SECRET>
    async def telegram_updates(request: web.Request) -> web.Response:
        # Проверка секрета от Telegram (мы его зададим в set_webhook)
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != BOT_SECRET:
            return web.Response(status=403, text="forbidden")
        data = await request.json()
        await ptb_app.update_queue.put(Update.de_json(data=data, bot=ptb_app.bot))
        return web.Response(text="OK")

    # 2) CRM webhook: /crmhook  (просто лог, можно расширять)
    async def crmhook(request: web.Request) -> web.Response:
        try:
            body = await request.read()
            log.info("CRM WEBHOOK | headers=%s | body=%s",
                     dict(request.headers), body.decode("utf-8", errors="ignore"))
        except Exception:
            log.exception("Failed to read CRM webhook")
        return web.Response(text="ok")

    # 3) healthz
    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post(f"/{BOT_SECRET}", telegram_updates)
    app.router.add_post("/crmhook", crmhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    return app

# -----------------------------
# MAIN (кастомный вебхук, без run_webhook)
# -----------------------------
async def main():
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    # handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all))

    # Готовим aiohttp сервер
    aio = make_aiohttp_app(application)
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    # Регистрируем вебхук в Telegram
    telegram_url = f"{PUBLIC_BASE_URL.rstrip('/')}/{BOT_SECRET}"
    log.info("Starting webhook on 0.0.0.0:%s, path: /%s", PORT, BOT_SECRET)
    log.info("Setting Telegram webhook to: %s", telegram_url)
    await application.bot.set_webhook(
        url=telegram_url,
        secret_token=BOT_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )

    # Запускаем PTB
    async with application:
        await application.start()
        try:
            # просто «спим», пока процесс живёт
            while True:
                await asyncio.sleep(3600)
        finally:
            await application.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if RO is not None:
            try:
                asyncio.run(RO.close())
            except Exception:
                pass
