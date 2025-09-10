# wa_server.py
import os
import httpx
from fastapi import FastAPI, Request, Response

APP = FastAPI()

WA_TOKEN = os.getenv("WA_TOKEN")              # токен Cloud API (временный или постоянный)
WA_PHONE_ID = os.getenv("WA_PHONE_ID")        # Phone Number ID
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "gncoverify")  # твой секрет для верификации вебхука

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

async def ai_reply(text: str, user_id: str) -> str:
    """
    Простейшая обёртка до OpenAI. Если ключа нет — эхо.
    """
    if not OPENAI_API_KEY:
        return f"Эхо: {text}"
    try:
        # Минимальный вызов Chat Completions
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "Ты вежливый русскоязычный ассистент сервиса проката/сервиса велосипедов."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Техническая пауза: {e}"

@APP.get("/whatsapp/webhook")
def verify(mode: str = None, challenge: str = None, token: str = None):
    # Meta дергает этот GET при подключении вебхука
    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return Response(content=challenge or "", media_type="text/plain")
    return Response(status_code=403)

@APP.post("/whatsapp/webhook")
async def incoming(request: Request):
    """
    Принимаем сообщения от Cloud API и отвечаем пользователю.
    """
    data = await request.json()
    entries = data.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            for msg in messages:
                if msg.get("type") == "text":
                    from_ = msg["from"]               # номер отправителя
                    body = msg["text"]["body"]        # текст
                    reply = await ai_reply(body, from_)
                    # Отправляем ответ пользователю через Cloud API
                    async with httpx.AsyncClient(timeout=15) as client:
                        await client.post(
                            f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages",
                            headers={"Authorization": f"Bearer {WA_TOKEN}"},
                            json={
                                "messaging_product": "whatsapp",
                                "to": from_,
                                "text": {"body": reply},
                            },
                        )
    return {"status": "ok"}
