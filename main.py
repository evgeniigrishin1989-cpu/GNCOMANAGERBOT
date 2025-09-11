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

# Паспорт компании и цены
COMPANY_NAME    = os.getenv("COMPANY_NAME", "GNCO")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "94 Hurd Street, Newton Park, PE")
CITY            = os.getenv("CITY", "Port Elizabeth (Gqeberha)")
WORKING_HOURS   = os.getenv("WORKING_HOURS", "Ежедневно 09:00–18:00")
WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "+27XXXXXXXXXX")
CURRENCY        = os.getenv("CURRENCY", "R")
TOW_PRICE_LOCAL = os.getenv("TOW_PRICE_LOCAL", "300")  # фикс по городу

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
URL_RE   = re.compile(r"https?://\S+", re.I)

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

def extract_map_link(text: str) -> Optional[str]:
    """Выцепляем ссылку на карту (google/yandex/waze/osm/2gis/what3words)."""
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    if re.search(r"(google\.com/maps|maps\.app\.goo\.gl|yandex\.(ru|com)/maps|waze\.com|osm\.org|openstreetmap|2gis|what3words)", url, re.I):
        return url
    return None

def friendly_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    # сначала берём сохранённое имя
    if context.user_data.get("name"):
        return context.user_data["name"]
    # затем — из Telegram-профиля (если есть)
    u = update.effective_user
    if not u:
        return ""
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name

# мини-история диалога
MAX_HISTORY = 6
def push_history(store: List[Dict[str, str]], role: str, content: str) -> None:
    if content:
        store.append({"role": role, "content": content.strip()[:2000]})
        while len(store) > MAX_HISTORY:
            store.pop(0)

# Запрет DIY (самостоятельный ремонт)
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
    "самостоятельному ремонту. Предлагаю быструю диагностику, запись в сервис и (при необходимости) эвакуатор. "
    "Коротко опишите симптомы — и я оформлю обращение."
)

# Фильтр «не обещай запись», пока нет заявки
BOOKING_PHRASES = r"(запис(ал|ываю)|оформ(ил|ляю)|постав(ил|лю)\s+в\s+расписание|созда(л|ю)\s+заявк)"
def sanitize_ai_reply(text: str, has_inquiry: bool) -> str:
    if has_inquiry:
        return text
    return re.sub(BOOKING_PHRASES + r"[^.!?]*", "", text, flags=re.I).strip()

# =========================
# БАЗА ЗНАНИЙ (KB)
# =========================
def tokens_ru(text: str) -> List[str]:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^a-zа-я0-9\s\-]+", " ", t)
    return [x for x in t.split() if x]

# Синонимы → канонические токены
CANON = {
    # цена
    "сколько": "цена", "стоит": "цена", "стоимость": "цена", "цена": "цена", "прайс": "цена",
    # эвакуатор / доставка
    "эвакуатор": "эвакуатор", "эвакуация": "эвакуатор", "забор": "эвакуатор",
    "доставка": "эвакуатор", "pickup": "эвакуатор", "пикап": "эвакуатор", "tow": "эвакуатор",
    # адрес / где / локация
    "адрес": "адрес", "где": "адрес", "находитесь": "адрес", "локация": "адрес",
    "местоположение": "адрес", "куда": "адрес", "как": "адрес", "доехать": "адрес", "добраться": "адрес",
    "карта": "адрес", "ссылку": "адрес", "ссылка": "адрес", "пин": "адрес", "координаты": "адрес",
    # диагностика
    "диагностика": "диагностика", "осмотр": "диагностика", "проверка": "диагностика",
    # запчасти
    "запчасти": "запчасти", "детали": "запчасти", "комплектующие": "запчасти", "наличие": "запчасти",
}
def canonical_tokens(text: str) -> List[str]:
    toks = tokens_ru(text)
    return [CANON.get(t, t) for t in toks]

def default_kb() -> List[Dict[str, Any]]:
    return [
        {
            "title": "Часы работы",
            "tags": ["часы", "время", "режим", "работы", "когда", "открыты", "выходные"],
            "answer": f"{COMPANY_NAME} работает: {WORKING_HOURS}. Адрес: {COMPANY_ADDRESS}."
        },
        {
            "title": "Адрес и как добраться",
            "tags": ["адрес", "где", "находимся", "локация", "как добраться", "местоположение", "карта", "куда ехать"],
            "answer": f"Наш адрес: {COMPANY_ADDRESS}. Город: {CITY}. Если неудобно ехать — можем забрать мотоцикл по городу за {CURRENCY}{TOW_PRICE_LOCAL}."
        },
        {
            "title": "Забор/доставка мотоцикла по городу",
            "tags": ["эвакуатор", "доставка", "забор", "по городу", "стоимость", "цена", "вызов", "эвакуация", "pickup", "tow"],
            "answer": f"В пределах {CITY} забор/доставка мотоцикла стоит фиксировано {CURRENCY}{TOW_PRICE_LOCAL}. За город — по расстоянию. Напишите район/адрес и время — всё организуем."
        },
        {
            "title": "Сроки ремонта",
            "tags": ["срок", "когда", "сколько времени", "готовность", "очередь", "время"],
            "answer": "Сроки зависят от загрузки и наличия запчастей. После первичной диагностики дадим точный план и сроки."
        },
        {
            "title": "Диагностика и стоимость",
            "tags": ["сколько стоит", "цена", "стоимость", "диагностика", "прайс", "расценки", "осмотр"],
            "answer": "Перед началом работ делаем осмотр и согласовываем бюджет. Финальная стоимость известна после диагностики."
        },
        {
            "title": "Запчасти и наличие",
            "tags": ["запчасти", "наличие", "детали", "комплектующие", "каталог", "заказ", "vin"],
            "answer": "Подберём детали по VIN/модели. Работаем с проверенными поставщиками; при необходимости закажем."
        },
        {
            "title": "Контакты",
            "tags": ["контакты", "связаться", "номер", "телефон", "ватсап", "whatsapp"],
            "answer": f"Быстрее всего — WhatsApp {WHATSAPP_NUMBER}. Можно писать и сюда в чат."
        },
        {
            "title": "Гарантия и качество",
            "tags": ["гарантия", "качество", "возврат", "повторный ремонт"],
            "answer": "Даем гарантию на выполненные работы и использованные запчасти. Все согласуем заранее."
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

def kb_search(query: str) -> Optional[str]:
    """Поиск по KB: канонизируем синонимы, усиливаем эвакуатор/адрес."""
    if not query:
        return None
    q = set(canonical_tokens(query))
    best_score, best_answer = 0, None
    for item in KB:
        tags_text = " ".join(item.get("tags", [])) + " " + item.get("title", "")
        t = set(canonical_tokens(tags_text))
        score = len(q & t)
        if score > best_score:
            best_score = score
            best_answer = item.get("answer")
    if ("адрес" in q or "эвакуатор" in q) and best_score >= 1:
        return best_answer
    return best_answer if best_score >= 2 else None

# Быстрые ответы без AI (интенты)
def quick_intent_answer(text: str) -> Optional[str]:
    q = set(canonical_tokens(text))
    if "эвакуатор" in q:
        return (f"По {CITY} забор/доставка мотоцикла — фикс {CURRENCY}{TOW_PRICE_LOCAL}. "
                f"За город — по расстоянию. Скиньте район/адрес и удобное время — всё организуем.")
    if "адрес" in q:
        return (f"Наш адрес: {COMPANY_ADDRESS}. Работаем: {WORKING_HOURS}. "
                f"Если неудобно ехать — можем забрать мотоцикл по городу за {CURRENCY}{TOW_PRICE_LOCAL}.")
    return None

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
# OpenAI (краткий ответ + анти-DIY + KB-контекст)
# =========================
async def ai_reply(user_text: str, history: List[Dict[str, str]]) -> str:
    if not OPENAI_API_KEY:
        return "Понимаю. Расскажите чуть подробнее — что случилось и какая модель? Если нужно, оформлю обращение."

    system = (
        "Ты дружелюбный менеджер сервиса {brand}. Отвечай естественно и тепло, 1–3 предложения. "
        "Никогда не выдумывай имя клиента: если имя неизвестно — не используй обращение по имени. "
        "Строгий запрет: не давай инструкций по самостоятельному ремонту/разборке/настройке. "
        "Вместо этого предлагай диагностику/запись/эвакуатор. "
        "Если канал WhatsApp — номер у нас уже есть, можно мягко спросить имя один раз. "
        "Используй факты из блока 'Контекст', если подходят."
    ).format(brand=COMPANY_NAME)

    kb_ctx = kb_search(user_text)
    context_block = f"Контекст: {kb_ctx}" if kb_ctx else "Контекст: (нет явных фактов)"

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
                if is_diy_request(text) or re.search(r"\b(открут|сним|установ|замен|подключ|раскрут|прижми|подтян)\w*\b", text.lower()):
                    return DIY_SAFE_REPLY
                return text
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 16); continue
            return f"Техническая ошибка AI: HTTP {r.status_code}"
        except Exception:
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 16)
    return "Сейчас высокая нагрузка. Давайте продолжим чат, а я попробую ещё раз."

# =========================
# Telegram/WhatsApp handlers
# =========================
WELCOME_TG = (
    f"Привет! Я менеджер {COMPANY_NAME}. Расскажите, что случилось — подскажу. "
    "Если готовы сразу оформить, пришлите номер в формате +27XXXXXXXXXX."
)
WELCOME_WA = (
    f"Привет! Я менеджер {COMPANY_NAME}. Расскажите, что случилось — подскажу. "
    "Можете также отправить пин или ссылку на местоположение для эвакуации."
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

# --- обработка геолокации (кнопка «Отправить местоположение» в TG)
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    context.user_data["pickup_location"] = {"lat": loc.latitude, "lon": loc.longitude}
    name = friendly_name(update, context)
    who = f", {name}" if name else ""
    txt = (f"Отлично{who}! Вижу точку на карте. По {CITY} эвакуатор — фикс {CURRENCY}{TOW_PRICE_LOCAL}. "
           "Подскажите, когда удобно забрать мотоцикл?")
    await update.message.reply_text(txt)

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

    # 1) DIY — мягкий отказ
    if is_diy_request(text):
        await update.message.reply_text(DIY_SAFE_REPLY)
        return

    # 1а) если прислали ссылку на карту — принимаем и спрашиваем время
    mlink = extract_map_link(text)
    if mlink:
        context.user_data["pickup_link"] = mlink
        name = friendly_name(update, context)
        who = f", {name}" if name else ""
        msg = (f"Спасибо{who}! Получил ссылку на локацию. По {CITY} заберём за {CURRENCY}{TOW_PRICE_LOCAL}. "
               "Когда удобно забрать мотоцикл?")
        await update.message.reply_text(msg)
        return

    # 2) Телеграм: если встретили номер — создаём лид
    phone = extract_phone(text) if CHANNEL == "telegram" else None
    if phone:
        name = context.user_data.get("name") or (friendly_name(update, context) or "Клиент")
        last_msgs = "\n".join([x["content"] for x in hist[-3:] if x["role"] == "user"])
        context.user_data["phone"] = phone
        if RO is None:
            await update.message.reply_text(
                f"Принял номер: <b>{phone}</b>. Зафиксировал запрос.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            inquiry = await RO.create_inquiry(
                contact_phone=phone,
                contact_name=name,
                title="Запрос на эвакуацию/ремонт",
                description=f"Источник: {ROAPP_SOURCE}. Недавние сообщения:\n{last_msgs}"[:900],
                location_id=int(ROAPP_LOCATION_ID) if ROAPP_LOCATION_ID else None,
                channel=ROAPP_SOURCE,
            )
            inq_id = inquiry.get("id")
            context.user_data["inquiry_id"] = inq_id
            await update.message.reply_text(
                "Готово! ✅ Заявка создана в CRM (ID: <b>{}</b>).\nНомер: <b>{}</b>\nИмя: <b>{}</b>".format(inq_id, phone, name),
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

    # 2а) Быстрые ответы (эвакуатор/адрес)
    qa = quick_intent_answer(text)
    if qa:
        # в WhatsApp, если имени ещё нет — попросим один раз
        if CHANNEL == "whatsapp" and not context.user_data.get("name"):
            context.user_data["await_name"] = True
            await update.message.reply_text(f"{qa}\n\nКак к вам обращаться?")
        else:
            await update.message.reply_text(qa)
        return

    # 3) KB → если нашли, ответим
    kb_answer = kb_search(text)
    if kb_answer:
        if CHANNEL == "whatsapp" and not context.user_data.get("name"):
            context.user_data["await_name"] = True
            await update.message.reply_text(f"{kb_answer}\n\nКак к вам обращаться?")
        else:
            await update.message.reply_text(kb_answer)
        return

    # 4) AI
    reply = await ai_reply(text, hist)
    reply = sanitize_ai_reply(reply, bool(context.user_data.get("inquiry_id")))
    push_history(hist, "assistant", reply)
    hint = maybe_phone_hint(context)
    final = reply if not hint else f"{reply}\n\n{hint}"
    await update.message.reply_text(final)

async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # подстрахуемся: если это текст — в текстовый хэндлер; если локация — в локационный
    if update.message:
        if update.message.location:
            await handle_location(update, context)
        elif update.message.text:
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
    # единый обработчик: и текст, и локации
    application.add_handler(MessageHandler(~filters.COMMAND, catch_all))

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
        allowed_updates=["message"],  # локации тоже приходят как message
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
