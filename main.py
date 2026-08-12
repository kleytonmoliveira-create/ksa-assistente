import os
import json
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

EXTRACTION_PROMPT = """
Analise a mensagem como registro operacional de manutenção naval.

Extraia SOMENTE informações explicitamente presentes na mensagem.

Regras importantes:
- Não invente equipamento, responsável, status ou falha.
- "Parece", "possivelmente", "aparenta" e similares são hipóteses.
- Só considere anomalia confirmada quando a mensagem afirmar isso como fato.
- Uma atividade é um trabalho executado, em execução ou planejado.
- Uma pendência é uma ação que ainda precisa ser feita.
- Não transforme conversa genérica ou pergunta técnica em registro operacional.
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


def get_active_project_id(whatsapp_id: str):
    response = (
        supabase
        .table("contacts")
        .select("active_project_id")
        .eq("whatsapp_id", whatsapp_id)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0].get("active_project_id")


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


def extract_operational_data(text: str):
    response = client.responses.create(
        model="gpt-5.6",
        instructions=EXTRACTION_PROMPT,
        input=text,
        text={
            "format": {
                "type": "json_schema",
                "name": "operational_record",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "activity": {
                            "type": ["object", "null"],
                            "properties": {
                                "description": {"type": "string"},
                                "equipment": {"type": ["string", "null"]},
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "planned",
                                        "in_progress",
                                        "completed",
                                        "unknown"
                                    ]
                                }
                            },
                            "required": [
                                "description",
                                "equipment",
                                "status"
                            ],
                            "additionalProperties": False
                        },

                        "issue": {
                            "type": ["object", "null"],
                            "properties": {
                                "description": {"type": "string"},
                                "equipment": {"type": ["string", "null"]},
                                "certainty": {
                                    "type": "string",
                                    "enum": [
                                        "confirmed",
                                        "suspected",
                                        "reported"
                                    ]
                                }
                            },
                            "required": [
                                "description",
                                "equipment",
                                "certainty"
                            ],
                            "additionalProperties": False
                        },

                        "pending_item": {
                            "type": ["object", "null"],
                            "properties": {
                                "description": {"type": "string"},
                                "responsible": {
                                    "type": ["string", "null"]
                                }
                            },
                            "required": [
                                "description",
                                "responsible"
                            ],
                            "additionalProperties": False
                        }
                    },

                    "required": [
                        "activity",
                        "issue",
                        "pending_item"
                    ],

                    "additionalProperties": False
                }
            }
        }
    )

    return json.loads(response.output_text)


def save_operational_data(
    project_id: int,
    whatsapp_id: str,
    message_id: str,
    data: dict
):
    activity = data.get("activity")

    if activity:
        supabase.table("activities").insert({
            "project_id": project_id,
            "whatsapp_id": whatsapp_id,
            "description": activity["description"],
            "equipment": activity.get("equipment"),
            "status": activity["status"],
            "source_message_id": message_id
        }).execute()

    issue = data.get("issue")

    if issue:
        supabase.table("issues").insert({
            "project_id": project_id,
            "whatsapp_id": whatsapp_id,
            "description": issue["description"],
            "equipment": issue.get("equipment"),
            "certainty": issue["certainty"],
            "status": "open",
            "source_message_id": message_id
        }).execute()

    pending = data.get("pending_item")

    if pending:
        supabase.table("pending_items").insert({
            "project_id": project_id,
            "whatsapp_id": whatsapp_id,
            "description": pending["description"],
            "responsible": pending.get("responsible"),
            "status": "open",
            "source_message_id": message_id
        }).execute()


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

        project_id = get_active_project_id(sender)

        if project_id:
            operational_data = extract_operational_data(
                received_text
            )

            print(
                "===== DADOS OPERACIONAIS ====="
            )
            print(operational_data)
            print(
                "=============================="
            )

            save_operational_data(
                project_id,
                sender,
                message_id,
                operational_data
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
