import asyncio
import json
import logging
import os
import re
import random
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

# КАНАЛ: telegram / whatsapp
CHANNEL         = os.getenv("CHANNEL", "telegram").lower().strip()

# «паспорт» компании для ответов
COMPANY_NAME    = os.getenv("COMPANY_NAME", "GNCO")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "ул. Примерная, 1")
WORKING_HOURS   = os.getenv("WORKING_HOURS", "Ежедневно 09:00–18:00")
CITY            = os.getenv("CITY", "Кейптаун")
WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "+27XXXXXXXXXX")

# CRM (RO App)
ROAPP_API_KEY     = os.getenv("ROAPP_API_KEY")
ROAPP_BASE_URL    = os.getenv("ROAPP_BASE_URL", "https://api.roapp.io")
ROAPP_LOCATION_ID = os.getenv("ROAPP_LOCATION_ID")
ROAPP_SOURCE      = os.getenv("ROAPP_SOURCE", "Telegram" if CHANNEL == "telegram" else "WhatsApp")

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
        return "Клиент"
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (u.username or f"id{u.id}")

# маленькая история
MAX_HISTORY = 6
def push_history(store: List[Dict[str, str]], role: str, content: str) -> None:
    if content:
        store.append({"role": role, "content": content.strip()[:2000]})
        while len(store) > MAX_HISTORY:
            store.pop(0)

# =========================
# БАЗА ЗНАНИЙ (KB)
# =========================
def default_kb() -> List[Dict[str, Any]]:
    return [
        {
            "title": "Часы работы",
            "tags": ["часы", "время", "режим", "работы", "когда", "открыты", "выходные"],
            "answer": f"{COMPANY_NAME} работает: {WORKING_HOURS}. Адрес: {COMPANY_ADDRESS}."
        },
        {
            "title": "Адрес и как добраться",
            "tags": ["адрес", "где", "находимся", "локация", "как добраться", "местоположение", "карта", "локацию"],
            "answer": f"Мы находимся: {COMPANY_ADDRESS}, {CITY}. Можем организовать эвакуатор — напишите, если нужно."
        },
        {
            "title": "Забор мотоцикла / эвакуатор",
            "tags": ["эвакуатор", "забрать", "забор", "доставка", "привезти", "самовывоз"],
            "answer": "Организуем забор мотоцикла эвакуатором. Сориентируем по стоимости и времени по адресу/району."
        },
        {
            "title": "Сроки ремонта",
            "tags": ["срок", "когда", "сколько времени", "готовность", "очередь", "время"],
            "answer": "Сроки зависят от загрузки и наличия запчастей. После первичной диагностики дадим точный план и сроки."
        },
        {
            "title": "Диагностика и стоимость",
            "tags": ["сколько стоит", "цена", "стоимость", "диагностика", "прайс", "расценки"],
            "answer": "Первично осматриваем и согласовываем работы/бюджет перед началом. Финальная стоимость — после диагностики."
        },
        {
            "title": "Запчасти и наличие",
            "tags": ["запчасти", "наличие", "детали", "комплектующие", "каталог", "заказ"],
            "answer": "Работаем с проверенными поставщиками. Подберём детали под VIN/модель; при необходимости заказ."
        },
        {
            "title": "Контакты",
            "tags": ["контакты", "связаться", "номер", "телефон", "ватсап", "whatsapp"],
            "answer": f"Быстрее всего — WhatsApp {WHATSAPP_NUMBER}. Также можно написать сюда в чат."
        },
        {
            "title": "Гарантия и качество",
            "tags": ["гарантия", "качество", "возврат", "повторный ремонт"],
            "answer": "Даем гарантию на выполненные работы и используемые запчасти. Все детали согласуем заранее."
        },
    ]

def load_external_kb(path: str = "kb.json") -> List[Dict[str, Any]]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        log.warning("KB load error: %s", e)
    return []

KB: List[Dict[str, Any]] = default_kb() + load_external_kb("kb.json")

def tokens_ru(text: str) -> List[str]:
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9\s\-]+", " ", t)
    return [x for x in t.split() if x]

def kb_search(query: str) -> Optional[str]:
    """Простейший лексический поиск по KB."""
    if not query:
        return None
    q = set(tokens_ru(query))
    best_score, best_answer = 0, None
    for item in KB:
        tags = " ".join(item.get("tags", [])) + " " + item.get("title", "")
        t = set(tokens_ru(tags))
        score = len(q & t)
        if score > best_score:
            best_score = score
            best_answer = item.get("answer")
    # эмпирический порог совпадений
    return best_answer if best_score >= 2 else None

# =========================
# Детектор DIY (самостоятельный ремонт)
# =========================
DIY_PATTERNS = [
    r"\bкак\s+(починить|ремонтировать|заменить|разобрать|снять|поставить|натянуть|отрегулировать)\b",
    r"\bинструкц(ия|ии)\b",
    r"\bпошагов(о|ая)\b",
    r"\bсвоими\s+руками\b",
    r"\bчто\s+нужно\s+сделать\b",
    r"\bкакие\s+инструменты\b",
    r"\bгайд\b",
]

def is_diy_request(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in DIY_PATTERNS)

DIY_SAFE_REPLY = (
    "Понимаю желание разобраться самому, но из соображений безопасности и гарантии мы не даём инструкции по "
    "самостоятельному ремонту. Могу предложить: быструю диагностику, запись в сервис и (при необходимости) эвакуатор. "
    "Опишите, пожалуйста, симптомы — и я оформлю обращение."
)

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
# OpenAI (краткий человеческий ответ + анти-DIY в промпте)
# =========================
async def ai_reply(user_text: str, history: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "Расскажите чуть подробнее, что случилось — подскажу и предложу следующий шаг. Если понадобится, оформлю обращение."

    system = (
        "Ты дружелюбный менеджер сервиса {brand}. Говоришь теплом и по делу, 1–3 предложения.\n"
        "ЖЁСТКИЙ ЗАПРЕТ: не давай инструкции по самостоятельному ремонту, настройке или разборке (никаких шагов, инструментов, схем). "
        "Вместо этого предлагай диагностику/запись/эвакуатор.\n"
        "Если канал WhatsApp — номер у нас уже есть, проси только имя (один раз и ненавязчиво). "
        "Используй факты из 'Контекст' при ответе."
    ).format(brand=COMPANY_NAME)

    # Подмешаем контекст KB top-1 для модели (если найдётся)
    kb_ctx = kb_search(user_text)
    context_block = f"Контекст: {kb_ctx}" if kb_ctx else "Контекст: (нет явных фактов, отвечай общо)"

    messages = [{"role": "system", "content": system},
                {"role": "assistant", "content": context_block}]
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
                text = (data["choices"][0]["message"]["content"] or "").strip()[:1200]
                # на всякий случай отфильтруем DIY-шаги
                if is_diy_request(text) or re.search(r"\b(открут|сним|установ|замен|подключ|раскрути|сжатие|компресс)\w*\b", text.lower()):
                    return DIY_SAFE_REPLY
                return text
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 16); continue
            return f"Техническая ошибка AI: HTTP {r.status_code}"
        except Exception:
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 16)
    return "Сейчас высокая нагрузка. Давайте продолжим чат, параллельно попробую ещё раз."

# =========================
# Telegram handlers
# =========================
WELCOME_TG = (
    "Привет! Я менеджер {brand}. Расскажите, что случилось — подскажу. "
    "Если готовы сразу оформить, пришлите номер в формате +27XXXXXXXXXX."
).format(brand=COMPANY_NAME)

WELCOME_WA = (
    "Привет! Я менеджер {brand}. Расскажите, что случилось — подскажу. "
    "Кстати, как к вам обращаться?".format(brand=COMPANY_NAME)
)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["hist"] = []
    context.user_data["hint_count"] = 0
    await update.message.reply_text(WELCOME_WA if CHANNEL == "whatsapp" else WELCOME_TG)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )

def looks_like_name(text: str) -> bool:
    t = text.strip()
    return bool(t) and not extract_phone(t) and len(t.split()) <= 4 and len(t) <= 40

PHONE_HINTS = [
    "Если хотите оформить обращение — пришлите номер в формате +XXXXXXXXXXX, всё сделаю.",
    "Готов оформить заявку — просто пришлите номер телефона в формате +XXXXXXXXXXX.",
    "Чтобы закрепить запрос и передать мастеру, нужен номер в формате +XXXXXXXXXXX.",
]
def maybe_phone_hint(context: ContextTypes.DEFAULT_TYPE) -> str:
    if CHANNEL == "whatsapp" or context.user_data.get("phone"):
        return ""
    cnt = int(context.user_data.get("hint_count", 0))
    context.user_data["hint_count"] = cnt + 1
    return random.choice(PHONE_HINTS) if cnt % 3 == 0 else ""

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    hist: List[Dict[str, str]] = context.user_data.get("hist") or []

    # если ждём имя (WA)
    if context.user_data.get("await_name"):
        if looks_like_name(text):
            context.user_data["name"] = text
            context.user_data["await_name"] = False
            await update.message.reply_text(f"Спасибо, {text}! Продолжайте — я помогу.")
        else:
            await update.message.reply_text("Как к вам обращаться? Имя можно одним словом 🙂")
        return

    push_history(hist, "user", text)
    context.user_data["hist"] = hist

    # DIY запрет — перехватываем сразу
    if is_diy_request(text):
        await update.message.reply_text(DIY_SAFE_REPLY)
        return

    # если видим номер (актуально для Telegram) — создаём лид
    phone = extract_phone(text) if CHANNEL == "telegram" else None
    if phone:
        name = context.user_data.get("name") or tg_display_name(update)
        last_msgs = "\n".join([x["content"] for x in hist[-3:] if x["role"] == "user"])
        context.user_data["phone"] = phone
        if RO is None:
            await update.message.reply_text(
                f"Принял номер: <b>{phone}</b>. Зафиксировал запрос.",
                parse_mode=ParseMode.HTML,
            )
            if CHANNEL == "whatsapp" and not context.user_data.get("name"):
                context.user_data["await_name"] = True
                await update.message.reply_text("Кстати, как к вам обращаться?")
            return
        try:
            inquiry = await RO.create_inquiry(
                contact_phone=phone,
                contact_name=name,
                title="Запрос на ремонт/запчасти",
                description=f"Источник: {ROAPP_SOURCE}. Недавние сообщения:\n{last_msgs}"[:900],
                location_id=int(ROAPP_LOCATION_ID) if ROAPP_LOCATION_ID else None,
                channel=ROAPP_SOURCE,
            )
            context.user_data["inquiry_id"] = inquiry.get("id")
            await update.message.reply_text(
                "Готово! ✅ Оформил обращение.\n"
                f"Номер: <b>{phone}</b>\nИмя: <b>{name}</b>",
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

    # БАЗА ЗНАНИЙ → если нашли — отвечаем ею
    kb_answer = kb_search(text)
    if kb_answer:
        # в WA попросим имя один раз
        if CHANNEL == "whatsapp" and not context.user_data.get("name"):
            context.user_data["await_name"] = True
            await update.message.reply_text(f"{kb_answer}\n\nКак к вам обращаться?")
        else:
            await update.message.reply_text(kb_answer)
        return

    # Иначе — AI
    reply = await ai_reply(text, hist)
    push_history(hist, "assistant", reply)
    hint = maybe_phone_hint(context)
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
        allowed_updates=["message"],  # только сообщения
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
