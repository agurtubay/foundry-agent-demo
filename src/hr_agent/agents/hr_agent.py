from __future__ import annotations

import json
import time
import uuid
import sys
from opentelemetry.trace import get_current_span
from typing import Optional

from azure.identity.aio import DefaultAzureCredential

from semantic_kernel.agents import AzureAIAgent, AzureAIAgentSettings, AzureAIAgentThread
from semantic_kernel.functions import kernel_function

from hr_agent.config import settings
from hr_agent.search.retriever import search_hr_chunks
from hr_agent.telemetry import get_tracer
from hr_agent.thread_store import ThreadStore
from hr_agent.cosmos_thread_store import CosmosThreadStore
from hr_agent.session_store import SessionStore

tracer = get_tracer("hr-agent.agent")

_cosmos_store = None

def _to_text(x) -> str:
    """Coerce SK response content (ChatMessageContent / list / etc.) into plain text."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "\n".join(_to_text(i) for i in x)
    # ChatMessageContent and many SK objects expose `.content`
    if hasattr(x, "content"):
        return _to_text(getattr(x, "content"))
    return str(x)

async def get_cosmos_store():
    global _cosmos_store
    if _cosmos_store is None:
        _cosmos_store = CosmosThreadStore.create_from_env()
    return _cosmos_store

class HRSearchPlugin:
    @kernel_function(
        name="search_hr_chunks",
        description="Search HR policy chunks in Azure AI Search. Input is a natural-language query."
    )
    def search_hr_chunks(self, query: str, top: int = 5) -> str:
        rows = search_hr_chunks(query=query, top=top)
        compact = [
            {
                "chunk_id": r["chunk_id"],
                "file": r["file"],
                "chunk": (r["chunk"] or "")[:1200],
            }
            for r in rows
        ]
        return json.dumps(compact, ensure_ascii=False)


async def _get_or_create_agent(client) -> AzureAIAgent:
    # Reuse existing Foundry agent if provided, else create a simple one
    if settings.agent_id:
        agent_definition = await client.agents.get_agent(settings.agent_id)
    else:
        agent_definition = await client.agents.create_agent(
            model=settings.model_deployment,
            name="HRPolicyAgent",
            instructions=(
                "You are an HR assistant. Use search_hr_chunks to retrieve policy text. "
                "Answer using the retrieved chunks and cite file + chunk_id. "
                "If you cannot find the answer, say so."
            ),
        )
    return AzureAIAgent(client=client, definition=agent_definition, plugins=[HRSearchPlugin()])


async def ask(
    question: str,
    thread_id: Optional[str] = None,
    reuse_thread: bool = True,
    stream: bool = False,
    cosmos_tid: Optional[str] = None
) -> tuple[str, Optional[str]]:
    """
    Returns (answer_text, thread_id_used).
    If reuse_thread=True, we load/save thread_id locally to skip create_thread overhead next runs.
    If stream=True, we stream incremental content to console and build the final answer text.
    """
    local_store = ThreadStore.default()
    session_id = SessionStore.default().load_or_create()
    cosmos = await get_cosmos_store()

    if reuse_thread and not thread_id:
        # 1) Cosmos
        cosmos_tid = await cosmos.get_thread_id(session_id)
        thread_id = cosmos_tid

        # 2) fallback local
        if not thread_id:
            thread_id = local_store.load()

    async with DefaultAzureCredential() as creds:
        async with AzureAIAgent.create_client(credential=creds, endpoint=settings.agent_endpoint) as client:
            agent = await _get_or_create_agent(client)

            thread = (
                AzureAIAgentThread(client=client, thread_id=thread_id)
                if thread_id
                else AzureAIAgentThread(client=client)
            )

            with tracer.start_as_current_span("agent.ask") as span:
                t_setup0 = time.perf_counter()
                span.add_event("start.ask")

                span.set_attribute("agent.id", agent.id)
                run_id = str(uuid.uuid4())
                span.set_attribute("run.id", run_id)
                span.set_attribute("question.len", len(question))
                
                # print trace_id for easy filtering in portal (take it from THIS span)
                span_ctx = span.get_span_context()
                trace_id_hex = format(span_ctx.trace_id, "032x")
                print(f"[run_id] {run_id}")
                print(f"[trace_id] {trace_id_hex}")


                if getattr(thread, "id", None):
                    span.set_attribute("thread.id", thread.id)

                span.add_event("thread.ready")

                t0 = time.perf_counter()

                if not stream:
                    span.add_event("before.get_response")
                    resp = await agent.get_response(messages=question, thread=thread)

                    span.add_event("after.get_response")
                    span.set_attribute("get_response_ms", int((time.perf_counter() - t0) * 1000))

                    tid = getattr(thread, "id", None) or getattr(getattr(resp, "thread", None), "id", None)

                    # ✅ thread id is definitive here
                    span.set_attribute("thread.id", tid or "")

                    if reuse_thread and tid:
                        # Local fallback (cheap) - always OK
                        local_store.save(tid)

                        # ✅ Cosmos write only if missing/changed
                        if not cosmos_tid or tid != cosmos_tid:
                            await cosmos.upsert_thread_id(session_id, tid)

                    elapsed_ms = int((time.perf_counter() - t0) * 1000)
                    span.set_attribute("agent.elapsed_ms", elapsed_ms)
                    answer_text = _to_text(getattr(resp, "content", resp))
                    return (answer_text, tid)
                
                # STREAMING MODE
                final_parts: list[str] = []
                first_token_ms: Optional[int] = None

                async for msg in agent.invoke(messages=question, thread=thread):
                    
                    text = getattr(msg, "content", None) or ""
                    if text:
                        if first_token_ms is None:
                            first_token_ms = int((time.perf_counter() - t0) * 1000)
                            span.set_attribute("agent.first_token_ms", first_token_ms)
                        print(text, end="", flush=True)
                        final_parts.append(text)

                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                span.set_attribute("agent.elapsed_ms", elapsed_ms)
                print() 

                tid = getattr(thread, "id", None)
                span.set_attribute("thread.id", tid or "")
                if reuse_thread and tid:
                    local_store.save(tid)
                    if not cosmos_tid or tid != cosmos_tid:
                        await cosmos.upsert_thread_id(session_id, tid)
                return ("".join(final_parts).strip(), tid)
