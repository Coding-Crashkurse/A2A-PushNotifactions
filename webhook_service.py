# supervisor/webhook_service.py

# ... (alle imports etc. bleiben gleich) ...
import uvicorn
from fastapi import FastAPI, Header, Response, status, Request
import json
import os
from datetime import datetime
import logging
import traceback

from a2a.types import Task, TextPart, Message
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill, Message, Role, TextPart,
    Task, TaskStatus, TaskState, Artifact, PushNotificationConfig,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBHOOK_HOST = "localhost"
WEBHOOK_PORT = 8001
EXPECTED_TOKEN = "my-super-secret-app-token"
RESULTS_DIR = "task_results"

app = FastAPI(title="Standalone Webhook Service")

def get_text_from_message(message: Message | None) -> str:
    if not message or not message.parts: return ""
    for part_wrapper in message.parts:
        part = part_wrapper.root
        if isinstance(part, TextPart): return part.text
    return ""

def get_final_answer_from_task(task: Task) -> str | None:
    if not task.artifacts: return None
    for artifact in task.artifacts:
        for part_wrapper in artifact.parts:
            part = part_wrapper.root
            if isinstance(part, TextPart): return part.text
    return None

@app.on_event("startup")
async def startup_event():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    logger.info(f"🚀 Webhook Service started. Listening on http://{WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logger.info(f"   Task logs will be saved in '{os.path.abspath(RESULTS_DIR)}/' directory.")

@app.post("/webhook")
async def receive_notification(
    task: Task, request: Request, response: Response, x_a2a_notification_token: str | None = Header(None)
):
    logger.info(f"🔔 INCOMING NOTIFICATION for Task ID: {task.id} from {request.client.host}")
    try:
        if x_a2a_notification_token != EXPECTED_TOKEN:
            logger.warning(f"🚨 REJECTED: Invalid token for Task {task.id}!")
            response.status_code = status.HTTP_403_FORBIDDEN
            return {"status": "error", "detail": "Invalid token"}

        logger.info(f"✅ Token for Task {task.id} validated successfully.")
        
        log_file_path = os.path.join(RESULTS_DIR, f"{task.id}.log")
        timestamp = datetime.now().isoformat()
        status_message = get_text_from_message(task.status.message)
        log_entry = f"[{timestamp}] State: {task.status.state.value} - Message: '{status_message}'\n"

        if task.status.state == TaskState.completed:
            final_answer = get_final_answer_from_task(task)
            if final_answer:
                log_entry += f"    └── FINAL ANSWER: '{final_answer}'\n"
        
        # --- KORREKTUR HIER ---
        # Explizit UTF-8 Encoding verwenden, um Unicode-Fehler zu vermeiden.
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(log_entry)

        logger.info(f"   -> Logged update for Task {task.id} to '{log_file_path}'")
        return {"status": "notification processed successfully"}

    except Exception as e:
        logger.error(f"🚨 Unhandled error while processing notification for Task {task.id}: {e}\n{traceback.format_exc()}")
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"status": "error", "detail": "Internal server error"}

if __name__ == "__main__":
    uvicorn.run(app, host=WEBHOOK_HOST, port=WEBHOOK_PORT)