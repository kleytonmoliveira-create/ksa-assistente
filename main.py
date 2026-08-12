import os
import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from openai import OpenAI

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

client = OpenAI()

SYSTEM_PROMPT = """
Você é o KSA Assistente, assistente operacional da KSA Service.

A KSA Service atua com manutenção e reparação naval e offshore,
incluindo hidráulica, mecânica, elétrica, guindastes, davits,
winches, HPUs e equipamentos de movimentação de carga.

Regras:
- Responda em português do Brasil.
- Seja direto, técnico e claro.
- Não invente informações.
- Diferencie fatos confirmados de hipóteses.
- Quando faltar informação importante, pergunte.
- Em assuntos técnicos, priorize segurança e boas práticas.
- Não trate uma suspeita como falha confirmada.
- Por enquanto, responda apenas à mensagem atual do usuário.
"""


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
        return PlainTextResponse(
            content=challenge or "",
            status_code=200
        )

    return Response(status_code=403)


def send_whatsapp_message(to: str, text: str):
    url = (
        f"https://graph.facebook.com/v25.0/"
        f"{PHONE_NUMBER_ID}/messages"
    )

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

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    print("===== RESPOSTA META =====")
    print(response.status_code)
    print(response.text)
    print("=========================")

    return response


def ask_openai(user_text: str):
    response = client.responses.create(
        model="gpt-5.6",
        instructions=SYSTEM_PROMPT,
        input=user_text,
    )

    return response.output_text


@app.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()

    print("===== WEBHOOK RECEBIDO =====")
    print(payload)
    print("============================")

    try:
        value = payload["entry"][0]["changes"][0]["value"]

        # Ignora status de mensagens enviadas
        if "messages" not in value:
            return {"status": "ok"}

        message = value["messages"][0]

        # Por enquanto trabalha apenas com texto
        if message.get("type") != "text":
            sender = message["from"]
            send_whatsapp_message(
                sender,
                "Por enquanto consigo responder apenas mensagens de texto."
            )
            return {"status": "ok"}

        sender = message["from"]
        received_text = message["text"]["body"]

        print("PERGUNTA:", received_text)

        ai_reply = ask_openai(received_text)

        print("RESPOSTA IA:", ai_reply)

        send_whatsapp_message(sender, ai_reply)

    except Exception as e:
        print("ERRO AO PROCESSAR WEBHOOK:", repr(e))

    return {"status": "ok"}
