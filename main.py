import os
import json
import requests
import os
import os
import tempfile
import unicodedata

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fpdf import FPDF
KSA_HEADER_PATH = os.path.join("static", "cabecalho_ksa.png")

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

def normalize_command(text: str):
    return text.strip()


def create_project_from_command(command: str):
    # Formato:
    # @KSA criar projeto Skandi Achiever - DOF

    content = command[len("@KSA criar projeto"):].strip()

    if not content:
        return None, "Informe o nome do projeto."

    client_name = None
    project_name = content

    if " - " in content:
        parts = content.split(" - ", 1)
        project_name = parts[0].strip()
        client_name = parts[1].strip()

    try:
        response = (
            supabase
            .table("projects")
            .insert({
                "name": project_name,
                "client": client_name,
                "status": "active"
            })
            .execute()
        )

        project = response.data[0]

        return project, None

    except Exception as e:
        print("ERRO CRIAR PROJETO:", repr(e))

        return None, (
            "Não consegui criar o projeto. "
            "Ele pode já existir."
        )


def find_project_by_name(name: str):
    response = (
        supabase
        .table("projects")
        .select("*")
        .ilike("name", f"%{name}%")
        .eq("status", "active")
        .limit(5)
        .execute()
    )

    return response.data or []


def set_active_project(whatsapp_id: str, project_id: int):
    (
        supabase
        .table("contacts")
        .update({
            "active_project_id": project_id
        })
        .eq("whatsapp_id", whatsapp_id)
        .execute()
    )


def get_active_project(whatsapp_id: str):
    response = (
        supabase
        .table("contacts")
        .select(
            "active_project_id"
        )
        .eq("whatsapp_id", whatsapp_id)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    project_id = response.data[0].get(
        "active_project_id"
    )

    if not project_id:
        return None

    project_response = (
        supabase
        .table("projects")
        .select("*")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )

    if not project_response.data:
        return None

    return project_response.data[0]


def get_open_pending_items(project_id: int):
    response = (
        supabase
        .table("pending_items")
        .select(
            "id,description,responsible,status,created_at"
        )
        .eq("project_id", project_id)
        .eq("status", "open")
        .order("created_at")
        .execute()
    )

    return response.data or []

from datetime import datetime, timezone, timedelta

BRAZIL_TZ = timezone(timedelta(hours=-3))


def today_start_iso():
    now = datetime.now(BRAZIL_TZ)

    start = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    return start.isoformat()


def get_today_activities(project_id: int):
    return (
        supabase
        .table("activities")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", today_start_iso())
        .order("created_at")
        .execute()
        .data
        or []
    )


def get_today_issues(project_id: int):
    return (
        supabase
        .table("issues")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", today_start_iso())
        .order("created_at")
        .execute()
        .data
        or []
    )


def get_today_pending(project_id: int):
    return (
        supabase
        .table("pending_items")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", today_start_iso())
        .order("created_at")
        .execute()
        .data
        or []
    )


def build_daily_summary(project: dict):
    project_id = project["id"]

    activities = get_today_activities(project_id)
    issues = get_today_issues(project_id)
    pending = get_today_pending(project_id)

    lines = [
        f"RESUMO DO DIA — {project['name']}"
    ]

    if project.get("vessel"):
        lines.append(
            f"Embarcação: {project['vessel']}"
        )

    if project.get("client"):
        lines.append(
            f"Cliente: {project['client']}"
        )

    lines.append("")

    # ATIVIDADES
    lines.append("ATIVIDADES")

    if activities:
        for index, item in enumerate(
            activities,
            start=1
        ):
            line = (
                f"{index}. {item['description']}"
            )

            if item.get("equipment"):
                line += (
                    f"\nEquipamento: "
                    f"{item['equipment']}"
                )

            if item.get("status"):
                line += (
                    f"\nStatus: "
                    f"{item['status']}"
                )

            lines.append(line)

    else:
        lines.append(
            "Nenhuma atividade registrada hoje."
        )

    lines.append("")

    # ANOMALIAS
    lines.append("ANOMALIAS / OCORRÊNCIAS")

    if issues:
        for index, item in enumerate(
            issues,
            start=1
        ):
            line = (
                f"{index}. {item['description']}"
            )

            if item.get("equipment"):
                line += (
                    f"\nEquipamento: "
                    f"{item['equipment']}"
                )

            if item.get("certainty"):
                line += (
                    f"\nClassificação: "
                    f"{item['certainty']}"
                )

            lines.append(line)

    else:
        lines.append(
            "Nenhuma anomalia registrada hoje."
        )

    lines.append("")

    # PENDÊNCIAS
    lines.append("PENDÊNCIAS")

    if pending:
        for index, item in enumerate(
            pending,
            start=1
        ):
            line = (
                f"{index}. {item['description']}"
            )

            if item.get("responsible"):
                line += (
                    f"\nResponsável: "
                    f"{item['responsible']}"
                )

            if item.get("status"):
                line += (
                    f"\nStatus: "
                    f"{item['status']}"
                )

            lines.append(line)

    else:
        lines.append(
            "Nenhuma pendência registrada hoje."
        )

    return "\n\n".join(lines)


def get_today_utc_range():
    now_br = datetime.now(BRAZIL_TZ)

    start_br = now_br.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    end_br = start_br.replace(
        hour=23,
        minute=59,
        second=59,
        microsecond=999999
    )

    start_utc = start_br.astimezone(
        timezone.utc
    ).isoformat()

    end_utc = end_br.astimezone(
        timezone.utc
    ).isoformat()

    return start_utc, end_utc

def get_dpr_today_data(project_id: int):
    start_utc, end_utc = get_today_utc_range()

    activities = (
        supabase
        .table("activities")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", start_utc)
        .lte("created_at", end_utc)
        .order("created_at")
        .execute()
        .data
        or []
    )

    issues = (
        supabase
        .table("issues")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", start_utc)
        .lte("created_at", end_utc)
        .order("created_at")
        .execute()
        .data
        or []
    )

    pending_items = (
        supabase
        .table("pending_items")
        .select("*")
        .eq("project_id", project_id)
        .gte("created_at", start_utc)
        .lte("created_at", end_utc)
        .order("created_at")
        .execute()
        .data
        or []
    )

    return {
        "activities": activities,
        "issues": issues,
        "pending_items": pending_items
    }


def pdf_safe_text(text):
    """
    Evita caracteres incompatíveis com as fontes
    padrão do FPDF.
    """
    if text is None:
        return ""

    text = str(text)

    replacements = {
        "–": "-",
        "—": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "•": "-"
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def safe_filename(text):
    if not text:
        return "projeto"

    normalized = unicodedata.normalize(
        "NFKD",
        text
    )

    normalized = normalized.encode(
        "ascii",
        "ignore"
    ).decode("ascii")

    allowed = []

    for char in normalized:
        if char.isalnum():
            allowed.append(char)
        elif char in (" ", "-", "_"):
            allowed.append("_")

    result = "".join(allowed)

    while "__" in result:
        result = result.replace("__", "_")

    return result.strip("_")[:50]


def add_ksa_header(pdf):
    if os.path.exists(KSA_HEADER_PATH):
        pdf.image(
            KSA_HEADER_PATH,
            x=10,
            y=8,
            w=190
        )

        # Ajuste este valor caso o seu
        # cabeçalho seja mais alto.
        pdf.set_y(42)

    else:
        pdf.set_font(
            "Helvetica",
            "B",
            16
        )

        pdf.cell(
            0,
            10,
            "KSA SERVICE",
            new_x="LMARGIN",
            new_y="NEXT",
            align="C"
        )

        pdf.ln(5)


def pdf_section_title(pdf, title):
    pdf.ln(3)

    pdf.set_font(
        "Helvetica",
        "B",
        11
    )

    pdf.set_fill_color(
        235,
        235,
        235
    )

    pdf.cell(
        0,
        8,
        pdf_safe_text(title),
        border=1,
        fill=True,
        new_x="LMARGIN",
        new_y="NEXT"
    )

    pdf.ln(2)


def pdf_item(pdf, number, text):
    pdf.set_font(
        "Helvetica",
        "",
        9
    )

    pdf.multi_cell(
        0,
        5,
        pdf_safe_text(
            f"{number}. {text}"
        )
    )

    pdf.ln(1)


def generate_dpr_pdf(project: dict):
    data = get_dpr_today_data(
        project["id"]
    )

    activities = data["activities"]
    issues = data["issues"]
    pending_items = data["pending_items"]

    now_br = datetime.now(BRAZIL_TZ)

    date_br = now_br.strftime(
        "%d/%m/%Y"
    )

    filename = (
        f"DPR_"
        f"{safe_filename(project.get('name'))}_"
        f"{now_br.strftime('%Y-%m-%d')}.pdf"
    )

    file_path = os.path.join(
        tempfile.gettempdir(),
        filename
    )

    pdf = FPDF(
        orientation="P",
        unit="mm",
        format="A4"
    )

    pdf.set_auto_page_break(
        auto=True,
        margin=15
    )

    pdf.set_margins(
        left=10,
        top=10,
        right=10
    )

    pdf.add_page()

    add_ksa_header(pdf)

    # TÍTULO
    pdf.set_font(
        "Helvetica",
        "B",
        14
    )

    pdf.cell(
        0,
        8,
        "DPR - RELATORIO DIARIO DE PROGRESSO",
        new_x="LMARGIN",
        new_y="NEXT",
        align="C"
    )

    pdf.ln(3)

    # DADOS PRINCIPAIS
    pdf.set_font(
        "Helvetica",
        "",
        9
    )

    project_name = pdf_safe_text(
        project.get("name")
        or "Nao informado"
    )

    vessel = pdf_safe_text(
        project.get("vessel")
        or "Nao informada"
    )

    client_name = pdf_safe_text(
        project.get("client")
        or "Nao informado"
    )

    info = [
        ["Data", date_br],
        ["Projeto", project_name],
        ["Embarcacao", vessel],
        ["Cliente", client_name]
    ]

    for label, value in info:
        pdf.set_font(
            "Helvetica",
            "B",
            9
        )

        pdf.cell(
            32,
            7,
            pdf_safe_text(label),
            border=1
        )

        pdf.set_font(
            "Helvetica",
            "",
            9
        )

        pdf.cell(
            158,
            7,
            pdf_safe_text(value),
            border=1,
            new_x="LMARGIN",
            new_y="NEXT"
        )

    # ATIVIDADES
    pdf_section_title(
        pdf,
        "1. ATIVIDADES EXECUTADAS"
    )

    if activities:
        for index, item in enumerate(
            activities,
            start=1
        ):
            description = item.get(
                "description"
            ) or "Sem descricao"

            equipment = item.get(
                "equipment"
            )

            status = item.get(
                "status"
            )

            text = description

            if equipment:
                text += (
                    f"\nEquipamento: "
                    f"{equipment}"
                )

            if status:
                text += (
                    f"\nStatus: "
                    f"{status}"
                )

            pdf_item(
                pdf,
                index,
                text
            )

    else:
        pdf_item(
            pdf,
            1,
            "Nenhuma atividade registrada no dia."
        )

    # ANOMALIAS
    pdf_section_title(
        pdf,
        "2. ANOMALIAS / CONSTATACOES"
    )

    if issues:
        for index, item in enumerate(
            issues,
            start=1
        ):
            description = item.get(
                "description"
            ) or "Sem descricao"

            equipment = item.get(
                "equipment"
            )

            certainty = item.get(
                "certainty"
            )

            status = item.get(
                "status"
            )

            text = description

            if equipment:
                text += (
                    f"\nEquipamento: "
                    f"{equipment}"
                )

            if certainty:
                text += (
                    f"\nClassificacao: "
                    f"{certainty}"
                )

            if status:
                text += (
                    f"\nStatus: "
                    f"{status}"
                )

            pdf_item(
                pdf,
                index,
                text
            )

    else:
        pdf_item(
            pdf,
            1,
            "Nenhuma anomalia registrada no dia."
        )

    # PENDÊNCIAS
    pdf_section_title(
        pdf,
        "3. PENDENCIAS / PROXIMAS ACOES"
    )

    if pending_items:
        for index, item in enumerate(
            pending_items,
            start=1
        ):
            description = item.get(
                "description"
            ) or "Sem descricao"

            responsible = item.get(
                "responsible"
            )

            status = item.get(
                "status"
            )

            text = description

            if responsible:
                text += (
                    f"\nResponsavel: "
                    f"{responsible}"
                )

            if status:
                text += (
                    f"\nStatus: "
                    f"{status}"
                )

            pdf_item(
                pdf,
                index,
                text
            )

    else:
        pdf_item(
            pdf,
            1,
            "Nenhuma pendencia registrada."
        )

    # ASSINATURA
    pdf.ln(12)

    pdf.set_font(
        "Helvetica",
        "",
        9
    )

    pdf.cell(
        80,
        5,
        "_" * 40,
        new_x="LMARGIN",
        new_y="NEXT"
    )

    pdf.set_font(
        "Helvetica",
        "B",
        9
    )

    pdf.multi_cell(
        100,
        5,
        pdf_safe_text(
            KSA_SIGNATURE
        )
    )

    pdf.ln(3)

    pdf.set_font(
        "Helvetica",
        "I",
        7
    )

    pdf.multi_cell(
        0,
        4,
        (
            "Documento gerado automaticamente pelo "
            "KSA Assistente com base nos registros "
            "operacionais armazenados no sistema."
        )
    )

    pdf.output(file_path)

    return file_path, filename


def upload_pdf_to_whatsapp(file_path):
    url = (
        f"https://graph.facebook.com/v25.0/"
        f"{PHONE_NUMBER_ID}/media"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_TOKEN}"
    }

    with open(file_path, "rb") as file:
        files = {
            "file": (
                os.path.basename(file_path),
                file,
                "application/pdf"
            )
        }

        data = {
            "messaging_product":
                "whatsapp"
        }

        response = requests.post(
            url,
            headers=headers,
            files=files,
            data=data,
            timeout=60
        )

    print(
        "===== UPLOAD PDF META ====="
    )

    print(response.status_code)
    print(response.text)

    print(
        "==========================="
    )

    response.raise_for_status()

    result = response.json()

    return result["id"]


def send_whatsapp_document(
    to,
    media_id,
    filename,
    caption=None
):
    url = (
        f"https://graph.facebook.com/v25.0/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_TOKEN}",

        "Content-Type":
            "application/json"
    }

    document = {
        "id": media_id,
        "filename": filename
    }

    if caption:
        document["caption"] = caption

    payload = {
        "messaging_product":
            "whatsapp",

        "recipient_type":
            "individual",

        "to":
            to,

        "type":
            "document",

        "document":
            document
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=60
    )

    print(
        "===== ENVIO PDF META ====="
    )

    print(response.status_code)
    print(response.text)

    print(
        "=========================="
    )

    response.raise_for_status()

    return response.json()


def generate_and_send_dpr(
    whatsapp_id
):
    project = get_active_project(
        whatsapp_id
    )

    if not project:
        send_whatsapp_message(
            whatsapp_id,
            (
                "Nenhum projeto esta ativo. "
                "Use @KSA usar projeto NOME "
                "antes de gerar o DPR."
            )
        )

        return

    pdf_path = None

    try:
        send_whatsapp_message(
            whatsapp_id,
            (
                f"Gerando DPR de hoje para "
                f"{project['name']}..."
            )
        )

        pdf_path, filename = (
            generate_dpr_pdf(
                project
            )
        )

        media_id = (
            upload_pdf_to_whatsapp(
                pdf_path
            )
        )

        today = datetime.now(
            BRAZIL_TZ
        ).strftime("%d/%m/%Y")

        caption = (
            f"DPR {today} - "
            f"{project['name']}"
        )

        send_whatsapp_document(
            whatsapp_id,
            media_id,
            filename,
            caption
        )

        send_whatsapp_message(
            whatsapp_id,
            "DPR enviado com sucesso."
        )

    except Exception as e:
        print(
            "ERRO DPR:",
            repr(e)
        )

        send_whatsapp_message(
            whatsapp_id,
            (
                "Nao consegui gerar ou enviar "
                "o DPR. Verifique os logs "
                "do servidor."
            )
        )

    finally:
        if (
            pdf_path
            and os.path.exists(pdf_path)
        ):
            try:
                os.remove(pdf_path)

            except Exception:
                pass
def handle_command(
    whatsapp_id: str,
    text: str
):
    command = normalize_command(text)

    lower = command.lower()

    # CRIAR PROJETO
    if lower.startswith("@ksa criar projeto"):
        project, error = create_project_from_command(
            command
        )

        if error:
            return error

        set_active_project(
            whatsapp_id,
            project["id"]
        )

        reply = (
            f"Projeto criado e ativado:\n"
            f"{project['name']}"
        )

        if project.get("client"):
            reply += (
                f"\nCliente: "
                f"{project['client']}"
            )

        return reply

    # USAR PROJETO
    if lower.startswith("@ksa usar projeto"):
        project_name = command[
            len("@KSA usar projeto"):
        ].strip()

        if not project_name:
            return (
                "Informe o nome do projeto."
            )

        projects = find_project_by_name(
            project_name
        )

        if not projects:
            return (
                "Não encontrei um projeto ativo "
                f"com '{project_name}'."
            )

        if len(projects) > 1:
            names = "\n".join(
                [
                    f"- {p['name']}"
                    for p in projects
                ]
            )

            return (
                "Encontrei mais de um projeto:\n"
                f"{names}\n\n"
                "Informe um nome mais específico."
            )

        project = projects[0]

        set_active_project(
            whatsapp_id,
            project["id"]
        )

        return (
            "Projeto ativo alterado para:\n"
            f"{project['name']}"
        )

    # PROJETO ATIVO
    if lower in [
        "@ksa projeto ativo",
        "@ksa projeto",
        "@ksa qual projeto está ativo",
        "@ksa qual projeto esta ativo"
    ]:
        project = get_active_project(
            whatsapp_id
        )

        if not project:
            return (
                "Nenhum projeto está ativo."
            )

        reply = (
            f"Projeto ativo:\n"
            f"{project['name']}"
        )

        if project.get("vessel"):
            reply += (
                f"\nEmbarcação: "
                f"{project['vessel']}"
            )

        if project.get("client"):
            reply += (
                f"\nCliente: "
                f"{project['client']}"
            )

        return reply

    # PENDÊNCIAS
    if lower in [
        "@ksa pendências",
        "@ksa pendencias",
        "@ksa listar pendências",
        "@ksa listar pendencias"
    ]:
        project = get_active_project(
            whatsapp_id
        )

        if not project:
            return (
                "Nenhum projeto está ativo."
            )

        pending = get_open_pending_items(
            project["id"]
        )

        if not pending:
            return (
                f"Não há pendências abertas em "
                f"{project['name']}."
            )

        lines = [
            f"Pendências — {project['name']}:"
        ]

        for index, item in enumerate(
            pending,
            start=1
        ):
            line = (
                f"{index}. "
                f"{item['description']}"
            )

            if item.get("responsible"):
                line += (
                    f"\nResponsável: "
                    f"{item['responsible']}"
                )

            lines.append(line)

        return "\n\n".join(lines)

    # RESUMO DO DIA
    if lower in [
        "@ksa resumo hoje",
        "@ksa resumo do dia",
        "@ksa resumo"
    ]:
        project = get_active_project(
            whatsapp_id
        )

        if not project:
            return (
                "Nenhum projeto está ativo."
            )

        return build_daily_summary(
            project
        )

    # DPR
    if lower in [
        "@ksa dpr hoje",
        "@ksa dpr",
        "@ksa gerar dpr"
    ]:
        project = get_active_project(
            whatsapp_id
        )

        if not project:
            return (
                "Nenhum projeto está ativo."
            )

        return build_dpr(
            project
        )
        # DPR PDF
    if lower in [
        "@ksa dpr hoje",
        "@ksa gerar dpr",
        "@ksa dpr pdf"
    ]:
        generate_and_send_dpr(
            whatsapp_id
        )

        return "__DPR_SENT__"
    return None
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

        # ---------------------------------
        # COMANDOS @KSA
        # ---------------------------------
        command_reply = handle_command(
    sender,
    received_text
)

if command_reply:
    save_message(
        sender,
        "user",
        received_text,
        message_id
    )

    # DPR ja foi enviado pela própria função
    if command_reply == "__DPR_SENT__":
        save_message(
            sender,
            "assistant",
            "DPR PDF gerado e enviado."
        )

        return {
            "status": "ok"
        }

    save_message(
        sender,
        "assistant",
        command_reply
    )

    send_whatsapp_message(
        sender,
        command_reply
    )

    return {
        "status": "ok"
    }

        # ---------------------------------
        # CONTATO
        # ---------------------------------
        contact_name = None

        contacts = value.get("contacts", [])

        if contacts:
            contact_name = (
                contacts[0]
                .get("profile", {})
                .get("name")
            )

        save_contact(
            sender,
            contact_name
        )

        # ---------------------------------
        # SALVA A MENSAGEM RECEBIDA
        # ---------------------------------
        save_message(
            sender,
            "user",
            received_text,
            message_id
        )

        # ---------------------------------
        # PROJETO ATIVO + EXTRAÇÃO
        # ---------------------------------
        project_id = get_active_project_id(
            sender
        )

        if project_id:
            operational_data = (
                extract_operational_data(
                    received_text
                )
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

        # ---------------------------------
        # RESPOSTA DA IA
        # ---------------------------------
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

def build_dpr(project: dict):
    project_id = project["id"]

    activities = get_today_activities(project_id)
    issues = get_today_issues(project_id)
    pending = get_today_pending(project_id)

    today = datetime.now(BRAZIL_TZ).strftime(
        "%d/%m/%Y"
    )

    lines = [
        "DPR — DAILY PROGRESS REPORT",
        "",
        f"Data: {today}",
        f"Projeto: {project['name']}",
    ]

    if project.get("vessel"):
        lines.append(
            f"Embarcação: {project['vessel']}"
        )

    if project.get("client"):
        lines.append(
            f"Cliente: {project['client']}"
        )

    lines.extend([
        "",
        "1. ATIVIDADES EXECUTADAS"
    ])

    if activities:
        for index, item in enumerate(
            activities,
            start=1
        ):
            lines.append(
                f"{index}. {item['description']}"
            )
    else:
        lines.append(
            "Nenhuma atividade registrada."
        )

    lines.extend([
        "",
        "2. ANOMALIAS / CONSTATAÇÕES"
    ])

    if issues:
        for index, item in enumerate(
            issues,
            start=1
        ):
            text = (
                f"{index}. "
                f"{item['description']}"
            )

            if item.get("certainty"):
                text += (
                    f" "
                    f"[{item['certainty']}]"
                )

            lines.append(text)
    else:
        lines.append(
            "Nenhuma anomalia registrada."
        )

    lines.extend([
        "",
        "3. PENDÊNCIAS / PRÓXIMAS AÇÕES"
    ])

    if pending:
        for index, item in enumerate(
            pending,
            start=1
        ):
            text = (
                f"{index}. "
                f"{item['description']}"
            )

            if item.get("responsible"):
                text += (
                    f" — Responsável: "
                    f"{item['responsible']}"
                )

            lines.append(text)
    else:
        lines.append(
            "Nenhuma pendência registrada."
        )

    lines.extend([
        "",
        "Documento gerado automaticamente "
        "pelo KSA Assistente."
    ])

    return "\n".join(lines)

def add_ksa_header(pdf):
    if os.path.exists(KSA_HEADER_PATH):
        pdf.image(
            KSA_HEADER_PATH,
            x=10,
            y=8,
            w=190
        )
        pdf.set_y(38)
    else:
        pdf.set_font(
            "Arial",
            "B",
            14
        )
        pdf.cell(
            0,
            10,
            "KSA Service",
            ln=True,
            align="C"
        )
        pdf.ln(5)
KSA_HEADER_PATH = "static/cabecalho_ksa.png"

BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")

KSA_SIGNATURE = (
    "Kleyton Oliveira - Diretor Tecnico KSA Service"
)
