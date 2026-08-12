import os
import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge or "", status_code=200)

    return Response(status_code=403)


def send_whatsapp_message(to: str, text: str):
    url = f"https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "body": text
        }
    }

    response = requests.post(url, headers=headers, json=payload, timeout=20)

    print("===== RESPOSTA META =====")
    print(response.status_code)
    print(response.text)
    print("=========================")

    return response


@app.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()

    print("===== WEBHOOK RECEBIDO =====")
    print(payload)
    print("============================")

    try:
        value = payload["entry"][0]["changes"][0]["value"]

        # Ignora webhooks de status de mensagens enviadas
        if "messages" not in value:
            return {"status": "ok"}

        message = value["messages"][0]

        # Por enquanto responde apenas texto
        if message.get("type") != "text":
            return {"status": "ok"}

        sender = message["from"]
        received_text = message["text"]["body"]

        reply = f"Recebi sua mensagem: {received_text}"

        send_whatsapp_message(sender, reply)

    except Exception as e:
        print("ERRO AO PROCESSAR WEBHOOK:", str(e))

    return {"status": "ok"}
