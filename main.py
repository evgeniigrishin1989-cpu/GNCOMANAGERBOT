import asyncio
import json
import logging
import os
import re
import random
from typing import Optional, List, Dict

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.request").setLevel(logging.WARNING)
log = logging.getLogger("gnco")

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
BOT_SECRET      = os.getenv("BOT_SECRET", "gncohook")
PUBLIC_BASE_URL = os.getenv("WEBHOOK_URL")
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
    if not (7 <= len(digits) <= 15):
        return None
    return f"+{digits}"

def extract_phone(text: str) -> Optional[str]:
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

# Небольшая «память» диалога
MAX_HISTORY = 6
def push_history(store: List[Dict[str, str]], role: str, content: str) -> None:
    if content:
        store.append({"role": role, "content": content.strip()[:2000]})
        while len(store) > MAX_HISTORY:
            store.pop(0)

# Варианты мягкого напоминания про телефон
PHONE_HINTS = [
    "Если хотите оформить обращение — пришлите номер в формате +XXXXXXXXXXX, всё сделаю.",
    "Готов оформить заявку — просто пришлите номер телефона в формате +XXXXXXXXXXX.",
    "Чтобы закрепить запрос и передать мастеру, нужен номер в формате +XXXXXXXXXXX.",
    "Могу оформить обращение прямо сейчас — номер нужен в виде +XXXXXXXXXXX.",
]

def next_phone_hint(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Показываем напоминание не чаще 1 раза в 3 ответа."""
    cnt = int(context.user_data.get("hint_count", 0))
    context.user_data["hint_count"] = cnt + 1
    if cnt % 3 == 0:  # 0,3,6,...
        return random.choice(PHONE_HINTS)
    return ""

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
# OpenAI (короткие живые ответы + ретраи)
# =========================
async def ai_reply(user_text: str, history: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        # Без ключа даём простой дружелюбный ответ
        return "Понимаю. Расскажите чуть подробнее — что случилось и какая модель? Если понадобится, оформлю заявку."

    system = (
        "Ты дружелюбный менеджер мотосервиса GNCO. Отвечай по-русски, естественно и тёпло, без канцелярита. "
        "Коротко: 1–3 предложения. Сначала проявляй участие, затем один-два уточняющих вопроса "
        "или конкретный совет по следующему шагу. Не проси номер телефона — этим займётся другой блок."
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text[:2000]})

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 300}

    backoff = 1
    for _ in range(5):
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
            return f"Техническая ошибка AI: HTTP {r.status_code}"
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 16)
    return "Сейчас высокая нагрузка. Давайте продолжим, а я параллельно попробую ещё раз."

# =========================
# Telegram handlers
# =========================
WELCOME = (
    "Привет! Я менеджер GNCO. Расскажите, что случилось с мотоциклом — подскажу и при необходимости оформлю обращение. "
    "Если готовы сразу оформить, пришлите номер в формате +27XXXXXXXXXX."
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["hist"] = []
    context.user_data["hint_count"] = 0
    await update.message.reply_text(WELCOME)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    hist: List[Dict[str, str]] = context.user_data.get("hist") or []
    push_history(hist, "user", text)
    context.user_data["hist"] = hist

    # 1) если видим номер — оформляем заявку
    phone = extract_phone(text)
    if phone:
        name = tg_display_name(update)
        last_msgs = "\n".join([x["content"] for x in hist[-3:] if x["role"] == "user"])
        # сброс частоты напоминаний
        context.user_data["hint_count"] = 0

        if RO is None:
            await update.message.reply_text(
                f"Принял номер: <b>{phone}</b>. Сейчас ключ CRM не настроен, но я зафиксировал запрос.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            inquiry = await RO.create_inquiry(
                contact_phone=phone,
                contact_name=name,
                title="Запрос на ремонт/запчасти",
                description=f"Источник: Telegram.\nНедавние сообщения:\n{last_msgs}"[:900],
                location_id=int(ROAPP_LOCATION_ID) if ROAPP_LOCATION_ID else None,
                channel=ROAPP_SOURCE,
            )
            context.user_data["phone"] = phone
            context.user_data["inquiry_id"] = inquiry.get("id")
            await update.message.reply_text(
                "Готово! ✅ Оформил обращение.\n"
                f"Номер: <b>{phone}</b>\nИмя: <b>{name}</b>\n"
                "Мастер свяжется, подскажет по срокам и стоимости.",
                parse_mode=ParseMode.HTML,
            )
            return
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(
                "❌ Не удалось создать заявку в CRM.\n"
                f"HTTP {e.response.status_code}\n{e.response.text[:600]}"
            )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Техническая ошибка: {e}")
            return

    # 2) иначе отвечаем как человек (AI)
    reply = await ai_reply(text, hist)
    push_history(hist, "assistant", reply)
    hint = next_phone_hint(context)
    final = reply if not hint else f"{reply}\n\n{hint}"
    await update.message.reply_text(final)

async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await handle_text(update, context)

# =========================
# AIOHTTP web app (Telegram + CRM + healthz)
# =========================
def make_aiohttp_app(ptb_app: Application) -> web.Application:
    app = web.Application()

    async def telegram_updates(request: web.Request) -> web.Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != BOT_SECRET:
            return web.Response(status=403, text="forbidden")
        data = await request.json()
        await ptb_app.update_queue.put(Update.de_json(data=data, bot=ptb_app.bot))
        return web.Response(text="OK")

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
# MAIN
# =========================
async def main():
    application = Application.builder().token(BOT_TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_all))

    aio = make_aiohttp_app(application)
    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    telegram_url = f"{PUBLIC_BASE_URL.rstrip('/')}/{BOT_SECRET}"
    log.info("Starting webhook on 0.0.0.0:%s, path: /%s", PORT, BOT_SECRET)
    log.info("Setting Telegram webhook to: %s", telegram_url)
    await application.bot.set_webhook(
        url=telegram_url,
        secret_token=BOT_SECRET,
        allowed_updates=["message"],
    )

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
