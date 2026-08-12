import os
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "ksa_webhook_2026")

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

@app.post("/webhook")
async def receive_webhook(request: Request):
    payload = await request.json()

    print("===== WEBHOOK RECEBIDO =====")
    print(payload)
    print("============================")

    return {"status": "ok"}
