import uvicorn
import asyncio
import httpx
from fastapi import FastAPI
from dotenv import load_dotenv
from uuid import uuid4
import traceback
import logging

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    Message,
    Role,
    TextPart,
    Task,
    TaskStatus,
    TaskState,
    Artifact,
    PushNotificationConfig,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7)
AGENT_HOST = "localhost"
AGENT_PORT = 8000
AGENT_URL = f"http://{AGENT_HOST}:{AGENT_PORT}"


def get_text_from_a2a_message(message: Message | None) -> str:
    if not message or not message.parts:
        return ""
    for part_wrapper in message.parts:
        actual_part = getattr(part_wrapper, "root", part_wrapper)
        if isinstance(actual_part, TextPart):
            return actual_part.text
    return ""


class PushNotificationExecutor(AgentExecutor):
    async def _send_progress_update(
        self, webhook_url: str, token: str | None, task: Task
    ):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-A2A-Notification-Token"] = token

        update_text = task.status.message.parts[0].root.text

        async with httpx.AsyncClient() as client:
            logger.info(
                f"[Task {task.id}] Sending update '{update_text}' to webhook..."
            )
            response = await client.post(
                webhook_url,
                content=task.model_dump_json(exclude_none=True),
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            logger.info(
                f"[Task {task.id}] Webhook service responded with {response.status_code}."
            )

    async def _run_llm_and_notify(
        self,
        context: RequestContext,
        push_config: PushNotificationConfig,
        user_query: str,
    ):
        logger.info(f"[Task {context.task_id}] Starting background LLM processing...")
        try:
            webhook_url = push_config.url
            token = push_config.token
            logger.info(
                f"[Task {context.task_id}] Will send multiple updates to webhook: {webhook_url}"
            )

            updates = [
                (2, "Warming up the AI engine..."),
                (2, "Checking knowledge base..."),
            ]
            for delay, text in updates:
                await asyncio.sleep(delay)
                progress_task = Task(
                    id=context.task_id,
                    contextId=context.context_id,
                    kind="task",
                    status=TaskStatus(
                        state=TaskState.working,
                        message=Message(
                            role=Role.agent,
                            parts=[TextPart(text=text)],
                            messageId=f"progress-{uuid4().hex}",
                        ),
                    ),
                )
                await self._send_progress_update(webhook_url, token, progress_task)

            logger.info(f"[Task {context.task_id}] Now calling the actual LLM...")
            result = await llm.ainvoke(
                [
                    SystemMessage(content="You are a helpful AI."),
                    HumanMessage(content=user_query),
                ]
            )
            answer = result.content
            logger.info(f"[Task {context.task_id}] LLM answered: '{answer[:50]}...'")

            await asyncio.sleep(2)
            final_artifact = Artifact(
                artifactId=f"art-{uuid4().hex}", parts=[TextPart(text=answer)]
            )
            final_task = Task(
                id=context.task_id,
                contextId=context.context_id,
                kind="task",
                status=TaskStatus(
                    state=TaskState.completed,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text="Task complete.")],
                        messageId=f"final-{uuid4().hex}",
                    ),
                ),
                artifacts=[final_artifact],
            )
            await self._send_progress_update(webhook_url, token, final_task)
            logger.info(
                f"✅ [Task {context.task_id}] All notifications sent. Background task finished."
            )

        except Exception as e:
            logger.error(
                f"🚨 [Task {context.task_id}] Error in background task: {e}\n{traceback.format_exc()}"
            )
            error_task = Task(
                id=context.task_id,
                contextId=context.context_id,
                kind="task",
                status=TaskStatus(
                    state=TaskState.failed,
                    message=Message(
                        role=Role.agent,
                        parts=[TextPart(text=f"An error occurred: {e}")],
                        messageId=f"error-{uuid4().hex}",
                    ),
                ),
            )
            if "webhook_url" in locals():
                await self._send_progress_update(webhook_url, token, error_task)

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        logger.info(f"Executing request for task {context.task_id}.")
        try:
            config = context.configuration
            if not config or not config.pushNotificationConfig:
                raise ValueError(
                    "MessageSendConfiguration with pushNotificationConfig is required."
                )
            push_config = config.pushNotificationConfig

            user_query = context.get_user_input()
            if not user_query:
                raise ValueError("No user message in text part found.")

            working_status = TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=Role.agent,
                    parts=[
                        TextPart(text="Request received. Processing in the background.")
                    ],
                    messageId=f"ack-{uuid4().hex}",
                ),
            )
            initial_task_response = Task(
                id=context.task_id,
                contextId=context.context_id,
                status=working_status,
                kind="task",
            )
            await event_queue.enqueue_event(initial_task_response)
            logger.info(
                f"✅ [Task {context.task_id}] Acknowledged request and sent 'working' status."
            )

            asyncio.create_task(
                self._run_llm_and_notify(context, push_config, user_query)
            )

        except Exception as e:
            logger.error(
                f"🚨 Error during initial request processing for Task {context.task_id}: {e}\n{traceback.format_exc()}"
            )
            raise e
        finally:
            await event_queue.close()

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        pass


def build_app() -> FastAPI:
    skill = AgentSkill(
        id="general_qa",
        name="General QA",
        description="Answers questions via multiple push notifications.",
        tags=["qa", "llm", "progress"],
    )
    card = AgentCard(
        name="Multi-Step Asynchronous QA Agent",
        description=skill.description,
        url=AGENT_URL,
        version="1.1",
        capabilities=AgentCapabilities(pushNotifications=True),
        skills=[skill],
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
    )
    agent_executor = PushNotificationExecutor()
    handler = DefaultRequestHandler(
        agent_executor=agent_executor, task_store=InMemoryTaskStore()
    )
    app = A2AStarletteApplication(agent_card=card, http_handler=handler).build()
    api = FastAPI(title="Server: Multi-Step Asynchronous QA Agent")
    api.mount("/", app)
    return api


app = build_app()

if __name__ == "__main__":
    print(
        f"🚀 Starting Multi-Step A2A Agent Server on http://{AGENT_HOST}:{AGENT_PORT}"
    )
    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT)
