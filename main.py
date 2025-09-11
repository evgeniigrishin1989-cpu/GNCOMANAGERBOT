import asyncio
import json
import logging
import os
import re
from typing import Optional, List, Dict, Any

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

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# Приглушим болтливые логгеры (не светим токены в URL)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
log = logging.getLogger("gnco")

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
BOT_SECRET      = os.getenv("BOT_SECRET", "gncohook")        # и путь вебхука, и secret_token
PUBLIC_BASE_URL = os.getenv("WEBHOOK_URL")                   # вида https://<render>.onrender.com
PORT            = int(os.getenv("PORT", "10000"))

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

ROAPP_API_KEY     = os.getenv("ROAPP_API_KEY")
ROAPP_BASE_URL    = os.getenv("ROAPP_BASE_URL", "https://api.roapp.io")
ROAPP_LOCATION_ID = os.getenv("ROAPP_LOCATION_ID")
ROAPP_SOURCE      = os.getenv("ROAPP_SOURCE", "Telegram")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not PUBLIC_BASE_URL:
    raise RuntimeError("WEBHOOK_URL is not set (e.g. https://<service>.onrender.com)")

# =========================
# ВСПОМОГАЛКИ
# =========================
PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{6,}")

def normalize_phone(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7 or len(digits) > 15:
        return None
    return f"+{digits}"

def extract_phone(text: str) -> Optional[str]:
    """Пробуем найти номер внутри свободного текста."""
    m = PHONE_RE.search(text or "")
    if not m:
        return None
    return normalize_phone(m.group(0))

def tg_display_name(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "Telegram User"
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (u.username or f"id{u.id}")

# Храним последние N фраз для краткого контекста AI
MAX_HISTORY = 6

def push_history(store: List[Dict[str, str]], role: str, content: str) -> None:
    if not content:
        return
    store.append({"role": role, "content": content.strip()[:2000]})
    # ограничим длину истории
    while len(store) > MAX_HISTORY:
        store.pop(0)

# =========================
# RO App client
# =========================
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

# =========================
# OpenAI (простой вызов с ретраями)
# =========================
async def ai_reply(user_text: str, history: List[Dict[str, str]]) -> str:
    """
    Лёгкий AI-ответ. История короткая, чтобы не ловить 429 по токенам.
    Ретраи на 429/5xx: 1s → 2s → 4s → 8s → 16s.
    """
    if not OPENAI_API_KEY:
        return "Я вас слышу. Могу продолжить как обычный ассистент, но ключ OpenAI не настроен. Пришлите номер телефона, и я создам заявку."

    messages = [{"role": "system",
                 "content": "Ты дружелюбный менеджер GNCO. Отвечай кратко и по делу. "
                            "Если пользователь пишет номер телефона, попроси подтвердить его и скажи, что создаёшь заявку."}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text[:2000]})

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.5, "max_tokens": 300}

    backoff = 1
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post("https://api.openai.com/v1/chat/completions",
                                      headers=headers, json=payload)
            if r.status_code == 200:
                data = r.json()
                return (data["choices"][0]["message"]["content"] or "").strip()[:1200]
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 16)
                continue
            # Прочие ошибки — отдадим короткое сообщение
            return f"Техническая ошибка AI: HTTP {r.status_code}"
        except Exception as e:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 16)
    return "Сейчас высокая нагрузка, не получилось получить ответ AI. Давайте попробуем ещё раз или пришлите номер телефона."

# =========================
# Telegram handlers
# =========================
WELCOME = (
    "Привет! Это менеджер GNCO. Напишите, что нужно — или пришлите номер в формате +27XXXXXXXXXX, "
    "и я сразу заведу заявку в CRM."
)
ASK_PHONE_HINT = "Если хотите оформить обращение, пришлите номер телефона в формате +XXXXXXXXXXX — я создам заявку."

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    uid = update.effective_user.id if update.effective_user else 0

    # Инициализируем историю
    hist: List[Dict[str, str]] = context.user_data.get("hist") or []
    push_history(hist, "user", text)
    context.user_data["hist"] = hist

    # 1) Если внутри текста видим телефон — создаём лид
    phone = extract_phone(text)
    if phone:
        name = tg_display_name(update)
        last_msgs = "\n".join([x["content"] for x in hist[-3:] if x["role"] == "user"])
        if RO is None:
            await update.message.reply_text(
                f"Телефон получил: <b>{phone}</b> ✅\n"
                "Ключ RO App не настроен, поэтому заявку в CRM не создаю.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            inquiry = await RO.create_inquiry(
                contact_phone=phone,
                contact_name=name,
                title="Запрос на ремонт байка",
                description=f"Источник: Telegram.\nПоследние сообщения:\n{last_msgs}".strip()[:900],
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
            return
        except httpx.HTTPStatusError as e:
            body = e.response.text
            await update.message.reply_text(
                "❌ Не удалось создать заявку в CRM.\n"
                f"HTTP {e.response.status_code}\n{body[:600]}"
            )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка интеграции: {e}")
            return

    # 2) Иначе — AI-ответ
    reply = await ai_reply(text, hist)
    push_history(hist, "assistant", reply)
    await update.message.reply_text(f"{reply}\n\n{ASK_PHONE_HINT}")

# На всякий случай — единый обработчик текста
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await handle_text(update, context)

# =========================
# AIOHTTP web app (Telegram + CRM + healthz)
# =========================
def make_aiohttp_app(ptb_app: Application) -> web.Application:
    app = web.Application()

    # Telegram webhook endpoint: /<BOT_SECRET>
    async def telegram_updates(request: web.Request) -> web.Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != BOT_SECRET:
            return web.Response(status=403, text="forbidden")
        data = await request.json()
        await ptb_app.update_queue.put(Update.de_json(data=data, bot=ptb_app.bot))
        return web.Response(text="OK")

    # CRM webhook: /crmhook (пока просто лог)
    async def crmhook(request: web.Request) -> web.Response:
        try:
            body = await request.read()
            log.info("CRM WEBHOOK | headers=%s | body=%s",
                     dict(request.headers), body.decode("utf-8", errors="ignore"))
        except Exception:
            log.exception("Failed to read CRM webhook")
        return web.Response(text="ok")

    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post(f"/{BOT_SECRET}", telegram_updates)
    app.router.add_post("/crmhook", crmhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", healthz)
    return app

# =========================
# MAIN: свой aiohttp-сервер + ручная регистрация вебхука
# =========================
async def main():
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    # handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all))

    # aiohttp-сервер
    aio = make_aiohttp_app(application)
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    # Регистрируем вебхук в Telegram (только обновления message — меньше дублей)
    telegram_url = f"{PUBLIC_BASE_URL.rstrip('/')}/{BOT_SECRET}"
    log.info("Starting webhook on 0.0.0.0:%s, path: /%s", PORT, BOT_SECRET)
    log.info("Setting Telegram webhook to: %s", telegram_url)
    await application.bot.set_webhook(
        url=telegram_url,
        secret_token=BOT_SECRET,
        allowed_updates=["message"],  # только сообщения
    )

    # Запускаем PTB
    async with application:
        await application.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await application.stop()
            if RO is not None:
                try:
                    await RO.close()
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())
