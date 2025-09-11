import os
import re
import json
import asyncio
from typing import Optional

import httpx
from aiohttp import web
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -----------------------------
# Env & config
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_SECRET = os.getenv("BOT_SECRET", "gncohook")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://<your>.onrender.com/gncohook

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

ROAPP_API_KEY = os.getenv("ROAPP_API_KEY")            # <-- from RO App Settings → API
ROAPP_BASE_URL = os.getenv("ROAPP_BASE_URL", "https://api.roapp.io")
ROAPP_LOCATION_ID = os.getenv("ROAPP_LOCATION_ID")    # optional
ROAPP_SOURCE = os.getenv("ROAPP_SOURCE", "Telegram")  # will be sent into inquiry as channel/source

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# -----------------------------
# Small helpers
# -----------------------------
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

def normalize_phone(raw: str) -> Optional[str]:
    """Return E.164-like +XXXXXXXXXXXX or None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    # add + if not present
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
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=30)

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
        """
        Docs: POST https://api.roapp.io/lead/
        Required: EITHER client_id OR (contact_phone + contact_name)
        We'll pass contact_phone + contact_name.
        """
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

    async def find_people(self, search: str, limit: int = 1) -> dict:
        """
        GET https://api.roapp.io/contacts/people?search=...&limit=...
        Use search to find person by phone/name.
        """
        params = {"search": search, "limit": limit}
        r = await self._client.get("/contacts/people", params=params)
        r.raise_for_status()
        return r.json()

RO = ROAppClient(ROAPP_API_KEY, ROAPP_BASE_URL) if ROAPP_API_KEY else None

# -----------------------------
# Telegram handlers
# -----------------------------
WELCOME = (
    "Ок, начнём. Введите номер телефона в формате +27XXXXXXXXXX "
    "— и я заведу заявку в CRM."
)

ASK_PHONE_AGAIN = "Пожалуйста, пришлите номер телефона в формате +XXXXXXXXXXX."

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) ждём телефон
    phone = normalize_phone(text)
    if not phone or not PHONE_RE.match(phone):
        await update.message.reply_text(ASK_PHONE_AGAIN)
        return

    # 2) нашли/создали лида в RO App
    name = tg_display_name(update)
    last_msg = context.user_data.get("last_msg") or ""

    if RO is None:
        await update.message.reply_text(
            "Телефон получил ✅\n"
            "RO App API ключ не настроен, поэтому создаю заявку только в чате."
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
        # запомним минимальную привязку
        context.user_data["phone"] = phone
        context.user_data["inquiry_id"] = inquiry.get("id")
        await update.message.reply_text(
            "Готово! ✅ Заявка создана в CRM.\n"
            f"Номер: <b>{phone}</b>\n"
            f"Имя: <b>{name}</b>",
            parse_mode=ParseMode.HTML,
        )
    except httpx.HTTPStatusError as e:
        body = e.response.text
        await update.message.reply_text(
            "❌ Не удалось создать заявку в CRM.\n"
            f"HTTP {e.response.status_code}\n{body[:600]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка интеграции: {e}")

async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # сохраним последний текст на случай, если номер придёт следующим сообщением
    if update.message and update.message.text:
        context.user_data["last_msg"] = update.message.text.strip()
    await handle_text(update, context)

# -----------------------------
# AIOHTTP web routes (for RO App webhooks, health, etc.)
# -----------------------------
async def roapp_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        data = {}
    # Здесь можно сделать уведомление в чат/лог — по вкусу.
    # Например, просто отдадим 200 OK:
    return web.json_response({"ok": True})

async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

def build_web_app() -> web.Application:
    app = web.Application()
    # Telegram webhook уже занят BOT_SECRET путём; наш — отдельный:
    app.router.add_post("/roapp-webhook", roapp_webhook)
    app.router.add_get("/", health)
    return app

# -----------------------------
# Main
# -----------------------------
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))

    # Ввод телефона и общий текст
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all))

    # AIOHTTP app для доп. маршрутов (RO App вебхук)
    web_app = build_web_app()

    if WEBHOOK_URL:
        # Telegram webhook mode (Render)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            secret_token=None,
            webhook_path=f"/{BOT_SECRET}",
            web_app=web_app,
        )
    else:
        # Local polling (для отладки)
        application.run_polling()

    # аккуратно закрываем HTTP клиент RO
    if RO is not None:
        asyncio.get_event_loop().run_until_complete(RO.close())

if __name__ == "__main__":
    main()
