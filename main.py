import os
import requests

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from openai import OpenAI
from supabase import create_client, Client

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

client = OpenAI()

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

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
- Não trate uma suspeita como falha confirmada.
- Considere o histórico da conversa fornecido.
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


def save_contact(whatsapp_id: str, name: str | None):
    supabase.table("contacts").upsert(
        {
            "whatsapp_id": whatsapp_id,
            "name": name
        },
        on_conflict="whatsapp_id"
    ).execute()


def save_message(
    whatsapp_id: str,
    role: str,
    content: str,
    whatsapp_message_id: str | None = None
):
    data = {
        "whatsapp_id": whatsapp_id,
        "role": role,
        "content": content
    }

    if whatsapp_message_id:
        data["whatsapp_message_id"] = whatsapp_message_id

    supabase.table("messages").insert(data).execute()


def get_history(whatsapp_id: str, limit: int = 20):
    response = (
        supabase
        .table("messages")
        .select("role,content,created_at")
        .eq("whatsapp_id", whatsapp_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    rows = response.data or []

    rows.reverse()

    return rows


def ask_openai(whatsapp_id: str):
    history = get_history(whatsapp_id)

    input_messages = []

    for item in history:
        input_messages.append({
            "role": item["role"],
            "content": item["content"]
        })

    response = client.responses.create(
        model="gpt-5.6",
        instructions=SYSTEM_PROMPT,
        input=input_messages
    )

    return response.output_text


def send_whatsapp_message(to: str, text: str):
    url = (
        f"https://graph.facebook.com/v25.0/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
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


@app.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()

    print("===== WEBHOOK RECEBIDO =====")
    print(payload)
    print("============================")

    try:
        value = payload["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            return {"status": "ok"}

        message = value["messages"][0]

        if message.get("type") != "text":
            return {"status": "ok"}

        sender = message["from"]
        message_id = message["id"]
        received_text = message["text"]["body"]

        contact_name = None

        contacts = value.get("contacts", [])

        if contacts:
            contact_name = (
                contacts[0]
                .get("profile", {})
                .get("name")
            )

        save_contact(sender, contact_name)

        save_message(
            sender,
            "user",
            received_text,
            message_id
        )

        ai_reply = ask_openai(sender)

        save_message(
            sender,
            "assistant",
            ai_reply
        )

        send_whatsapp_message(
            sender,
            ai_reply
        )

    except Exception as e:
        print(
            "ERRO AO PROCESSAR WEBHOOK:",
            repr(e)
        )

    return {"status": "ok"}   
