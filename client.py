# supervisor/cli_client.py

# ... (alle imports etc. bleiben gleich) ...
import asyncio
import httpx
from uuid import uuid4
import traceback
import logging

from a2a.client import A2AClient
from a2a.types import (
    Message, MessageSendParams, SendMessageRequest, TextPart, Role, Task,
    PushNotificationConfig, MessageSendConfiguration, SendMessageResponse
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

AGENT_BASE_URL = "http://localhost:8000"
WEBHOOK_URL = "http://localhost:8001/webhook"
NOTIFICATION_TOKEN = "my-super-secret-app-token"

async def main():
    logger.info(f"➡️  Connecting to Agent at {AGENT_BASE_URL}...")
    try:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            client = await A2AClient.get_client_from_agent_card_url(
                httpx_client=http_client,
                base_url=AGENT_BASE_URL,
            )

            if not client.agent_card.capabilities.pushNotifications:
                logger.error("🚨 Agent does not support Push Notifications. Exiting.")
                return

            logger.info(f"✅ Connected to Agent: '{client.agent_card.name}'.")
            print("   Type your query and press Enter. Type 'exit' to quit.")

            while True:
                user_input = await asyncio.to_thread(input, "You: ")
                if user_input.lower() in ["exit", "quit"]: break

                user_message = Message(role=Role.user, parts=[TextPart(text=user_input)], messageId=f"msg-{uuid4().hex}")
                push_config = PushNotificationConfig(url=WEBHOOK_URL, token=NOTIFICATION_TOKEN)
                send_config = MessageSendConfiguration(acceptedOutputModes=["text/plain"], pushNotificationConfig=push_config)
                
                request_id = f"request-{uuid4().hex}"
                request = SendMessageRequest(params=MessageSendParams(message=user_message, configuration=send_config), id=request_id)

                logger.info(f'▶️  Sending request {request_id} for "{user_input}"...')
                response = await client.send_message(request)

                # --- KORREKTUR HIER ---
                # Wir prüfen, ob die Antwort ein SendMessageResponse-Wrapper ist
                # und ob das `result` darin ein Task-Objekt ist.
                task_object = None
                if isinstance(response, SendMessageResponse):
                    task_object = response.root.result
                elif isinstance(response, Task): # Fallback, falls es doch direkt kommt
                    task_object = response

                if isinstance(task_object, Task):
                    logger.info(f"✅ Request Acknowledged. Task ID: {task_object.id}. Agent is working.")
                    logger.info("   The result will be sent to the webhook service.")
                else:
                    logger.error(f"🚨 Received an unexpected response. Content: {response}")

    except httpx.ConnectError:
        logger.error(f"\n🚨 Connection Error: Could not connect to {AGENT_BASE_URL}. Is the service running?")
    except Exception as e:
        logger.error(f"\n🚨 An unexpected error occurred: {type(e).__name__}: {e}")
        print(traceback.format_exc())
    
    logger.info("\n👋 Client is shutting down.")

if __name__ == "__main__":
    asyncio.run(main())