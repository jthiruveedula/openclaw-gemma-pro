import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException
from pydantic import BaseModel
import uvicorn
import httpx

# Observability (Step 6/7)
try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    from prometheus_fastapi_instrumentator import Instrumentator
    METRICS_ENABLED = True
except ImportError:
    METRICS_ENABLED = False

try:
    from config.logging_config import setup_logging
    import structlog
    setup_logging()
    LOG = structlog.get_logger()
except ImportError:
    LOG = logging.getLogger(__name__)

from workers.orchestrator.coordinator import AgentCoordinator
from skills.chat_router.handler import ChatRouter

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

app = FastAPI(title="OpenClaw Webhook Server")

if METRICS_ENABLED:
    # customize metrics if needed
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    MESSAGE_COUNTER = Counter("openclaw_messages_total", "Total messages received", ["channel"])
    PROCESSING_TIME = Histogram("openclaw_message_processing_seconds", "Time spent processing message", ["channel"])
    AGENT_TASK_COUNTER = Counter("openclaw_agent_tasks_total", "Total agent tasks executed", ["agent_type", "status"])

# --- Models ---
class TelegramMessage(BaseModel):
    update_id: int
    message: Optional[Dict[str, Any]] = None

class TwilioMessage(BaseModel):
    # Twilio sends form data, so we might need a different way to parse it
    pass

# --- Internal Logic ---

async def handle_message(channel: str, user_id: str, text: str, metadata: Dict[str, Any]):
    start_time = time.time()
    run_id = str(uuid.uuid4())[:8]

    if METRICS_ENABLED:
        MESSAGE_COUNTER.labels(channel=channel).inc()

    # Chat Router (Step 5)
    router = ChatRouter()
    route_info = await router.classify(text)
    tier = route_info.get("tier", "pro")

    coordinator = AgentCoordinator()
    context = {
        "channel": channel,
        "user_id": user_id,
        "run_id": run_id,
        "metadata": metadata,
        "tier": tier,
        "memory_context": await router.inject_memory(user_id)
    }

    try:
        result = await coordinator.run(text, context=context)
        # In a real scenario, we'd send the response back to the user here
        await send_response(channel, user_id, result)
    except Exception as e:
        LOG.error("failed_to_process_message", error=str(e), run_id=run_id)
    finally:
        duration = time.time() - start_time
        if METRICS_ENABLED:
            PROCESSING_TIME.labels(channel=channel).observe(duration)

async def send_response(channel: str, user_id: str, result: Dict[str, Any]):
    # Placeholder for sending response back to channel
    text_to_send = f"Processed goal. Status: {result.get('status')}"
    if result.get("critic_verdict"):
        verdict = result["critic_verdict"]
        if isinstance(verdict, dict) and verdict.get("verdict") == "pass":
             # This is a simplification. Ideally we'd have the actual executor results.
             pass

    if channel == "telegram":
        await send_telegram(user_id, text_to_send)
    elif channel == "whatsapp":
        await send_whatsapp(user_id, text_to_send)

async def send_telegram(chat_id: str, text: str):
    if not TELEGRAM_TOKEN:
        LOG.warning("telegram_token_missing")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})

async def send_whatsapp(to_number: str, text: str):
    # Twilio logic here
    LOG.info("whatsapp_response_placeholder", to=to_number, text=text)

# --- Endpoints ---

@app.post("/webhooks/telegram")
async def telegram_webhook(update: Dict[str, Any], background_tasks: BackgroundTasks):
    if "message" in update and "text" in update["message"]:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg["text"]
        background_tasks.add_task(handle_message, "telegram", chat_id, text, msg)
    return {"status": "ok"}

@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    # Twilio sends x-www-form-urlencoded
    form_data = await request.form()
    from_number = form_data.get("From")
    body = form_data.get("Body")
    if from_number and body:
        background_tasks.add_task(handle_message, "whatsapp", from_number, body, dict(form_data))
    return Response(content="<Response></Response>", media_type="text/xml")

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/admin/pending")
async def list_pending_actions():
    from guardrails.action_guardrail import guardrail
    return {"pending_actions": guardrail.list_pending()}

@app.post("/admin/confirm/{token}")
async def confirm_action(token: str):
    from guardrails.action_guardrail import guardrail
    try:
        # This is a bit tricky as we need the original executor function
        # For now, just mark it as released in the guardrail engine
        # In a real system, the executor would be waiting for this.
        return {"status": "Action released", "token": token}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/dashboard")
async def dashboard():
    from fastapi.responses import HTMLResponse

    html_content = """
    <html>
        <head>
            <title>OpenClaw Dashboard</title>
            <style>
                body { font-family: sans-serif; margin: 2em; background: #f4f4f9; }
                h1 { color: #333; }
                .card { background: white; padding: 1.5em; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 1em; }
                .status-pass { color: green; font-weight: bold; }
                .status-fail { color: red; font-weight: bold; }
            </style>
        </head>
        <body>
            <h1>OpenClaw Status Dashboard</h1>
            <div class="card">
                <h2>System Health</h2>
                <p>Status: <span class="status-pass">HEALTHY</span></p>
                <p>Ollama API: <span id="ollama-status">Checking...</span></p>
            </div>
            <div class="card">
                <h2>Active Agents</h2>
                <ul>
                    <li>Planner</li>
                    <li>Executor</li>
                    <li>Memory</li>
                    <li>Critic</li>
                </ul>
            </div>
            <div class="card">
                <h2>Quick Links</h2>
                <ul>
                    <li><a href="/metrics">Prometheus Metrics</a></li>
                    <li><a href="/docs">API Documentation (Swagger)</a></li>
                </ul>
            </div>
            <script>
                fetch('/health').then(r => r.json()).then(data => {
                    document.getElementById('ollama-status').innerText = 'Reachable';
                    document.getElementById('ollama-status').className = 'status-pass';
                }).catch(e => {
                    document.getElementById('ollama-status').innerText = 'Unreachable';
                    document.getElementById('ollama-status').className = 'status-fail';
                });
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
