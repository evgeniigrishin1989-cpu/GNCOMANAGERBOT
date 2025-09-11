# main.py
import os
import re
import json
import hmac
import hashlib
import asyncio
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -----------------------------
# Base config & logging
# -----------------------------
load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("gnco")

BOT_TOKEN = os.getenv("BOT_TOKEN")                         # Telegram bot token
BOT_SECRET = os.getenv("BOT_SECRET", "gncohook")           # путь и секрет вебхука Telegram
PUBLIC_BASE_URL = os.getenv("WEBHOOK_URL", "").rstrip("/") # https://gncomanagerbot-1.onrender.com
PORT = int(os.getenv("PORT", "10000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")               # не используем здесь (чтобы не ловить 429)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# CRM подпись (по желанию, тогда проверяется HMAC)
CRM_SECRET = os.getenv("CRM_SECRET", "")

# RO App (CRM) – для создания лидов по телефону из Telegram
ROAPP_API_KEY     = os.getenv("ROAPP_API_KEY")
ROAPP_BASE_URL    = os.getenv("ROAPP_BASE_URL", "https://api.roapp.io")
ROAPP_LOCATION_ID = os.getenv("ROAPP_LOCATION_ID")         # опционально
ROAPP_SOURCE      = os.getenv("ROAPP_SOURCE", "Telegram")  # канал/источник

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not PUBLIC_BASE_URL:
    raise RuntimeError("WEBHOOK_URL (public base) not set, e.g. https://<app>.onrender.com")

# Полный адрес вебхука Telegram:
TELEGRAM_WEBHOOK_URL = f"{PUBLIC_BASE_URL}/{BOT_SECRET}"

# -----------------------------
# Helpers
# -----------------------------
PHONE_RE = re.compile(r"^\+?\d{7,15}$")

def normalize_phone(raw: str) -> Optional[str]:
    """Return +E.164-like or None."""
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

def _get_sig_from_headers(headers) -> Optional[str]:
    """Достаём подпись из разных возможных заголовков."""
    for key in (
        "X-Signature-SHA256",
        "X-Hub-Signature-256",
        "X-CRM-Signature",
        "X-Signature",
        "X-Hub-Signature",
    ):
        val = headers.get(key)
        if val:
            # может приходить вида "sha256=abcdef..."
            return val.split("=", 1)[-1].strip()
    return None

def _check_hmac(raw_body: bytes, headers) -> bool:
    """Проверяем HMAC-SHA256, если задан CRM_SECRET. Если секрета нет — пропускаем проверку."""
    if not CRM_SECRET:
        return True  # проверка отключена
    their = _get_sig_from_headers(headers)
    if not their:
        return False
    my = hmac.new(CRM_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my, their)

# -----------------------------
# RO App client (httpx)
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
    "Ок, начнём. Пришлите номер телефона в формате +27XXXXXXXXXX — "
    "и я создам заявку в CRM."
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
    phone = normalize_phone(text)

    if not phone or not PHONE_RE.match(phone):
        # запомним фразу для описания лида (если номер пришлют следом)
        context.user_data["last_msg"] = text
        await update.message.reply_text(ASK_PHONE_AGAIN)
        return

    name = tg_display_name(update)
    last_msg = context.user_data.get("last_msg") or ""

    if RO is None:
        await update.message.reply_text(
            "Телефон получил ✅\n"
            "Ключ RO App не настроен — создаю заявку только в чате."
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
        body = e.response.text
        await update.message.reply_text(
            "❌ Не удалось создать заявку в CRM.\n"
            f"HTTP {e.response.status_code}\n{body[:600]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка интеграции: {e}")

async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # сохраняем последний текст — вдруг номер придёт следом
    if update.message and update.message.text:
        context.user_data["last_msg"] = update.message.text.strip()
    await handle_text(update, context)

# -----------------------------
# aiohttp web routes
# -----------------------------
async def roapp_webhook(request: web.Request) -> web.Response:
    # Заглушка под входящие события из RO App (если пригодится)
    try:
        data = await request.json()
    except Exception:
        data = {}
    log.info("ROAPP WEBHOOK: %s", json.dumps(data)[:500])
    return web.json_response({"ok": True})

async def crm_webhook(request: web.Request) -> web.Response:
    raw = await request.read()
    ok = _check_hmac(raw, request.headers)
    if not ok:
        log.warning("CRM signature failed")
        return web.Response(text="unauthorized", status=401)

    # Для отладки просто логируем и отвечаем 200
    # Здесь позже можно поставить задачу в очередь/обработать событие
    # и сделать upsert клиента по телефону
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        data = {}
    log.info("CRM WEBHOOK OK: %s", json.dumps(data)[:800])
    return web.Response(text="ok")

async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def root(request: web.Request) -> web.Response:
    return web.Response(text="OK")

def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/roapp-webhook", roapp_webhook)
    app.router.add_post("/crmhook", crm_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", root)
    return app

# -----------------------------
# Main
# -----------------------------
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))

    # Текст/телефон
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all))

    # AIOHTTP web app с /crmhook и /healthz
    web_app = build_web_app()

    # Запуск вебхука Telegram
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_SECRET,                   # путь: /<BOT_SECRET>
        webhook_url=TELEGRAM_WEBHOOK_URL,     # полный публичный URL
        secret_token=BOT_SECRET,              # Telegram пришлёт его в заголовке
        web_app=web_app,                      # наш aiohttp-приложение
    )

    # Аккуратно закрыть HTTP-клиент RO
    if RO is not None:
        try:
            asyncio.get_event_loop().run_until_complete(RO.close())
        except Exception:
            pass

if __name__ == "__main__":
    main()
