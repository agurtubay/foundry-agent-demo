from __future__ import annotations

import json
import time
import uuid
import sys
from opentelemetry.trace import get_current_span
from typing import Optional, AsyncGenerator, Union

from azure.identity.aio import DefaultAzureCredential

from semantic_kernel.agents import AzureAIAgent, AzureAIAgentSettings, AzureAIAgentThread
from semantic_kernel.functions import kernel_function

from hr_agent.config import settings
from hr_agent.search.retriever import search_hr_chunks
from hr_agent.telemetry import get_tracer
from hr_agent.cosmos_thread_store import CosmosThreadStore
from hr_agent.session_store import SessionStore

tracer = get_tracer("hr-agent.agent")

_cosmos_store = None
_cached_credential: DefaultAzureCredential | None = None
_cached_client = None
_cached_agent_definition = None

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


async def _get_or_create_agent_definition(client):
    global _cached_agent_definition
    # Cache only the agent definition, not the agent wrapper
    if _cached_agent_definition is not None:
        return _cached_agent_definition
    
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
    _cached_agent_definition = agent_definition
    return _cached_agent_definition


async def ask(
    question: str,
    thread_id: Optional[str] = None,
    reuse_thread: bool = True,
    stream: bool = False,
    cosmos_tid: Optional[str] = None,
    session_id: Optional[str] = None
) -> Union[tuple[str, Optional[str]], AsyncGenerator[dict, None]]:
    """
    Ask the HR agent a question.
    
    When stream=False: Returns tuple (answer_text, thread_id_used).
    When stream=True: Returns async generator yielding dict chunks:
        - {"type": "chunk", "content": "text"}
        - {"type": "done", "thread_id": "..."}
    
    If reuse_thread=True, we load/save thread_id from Cosmos to skip create_thread overhead next runs.
    If session_id is not provided, it will be loaded from/created in local storage (for CLI mode).
    """
    # Only use local session store if session_id is not explicitly provided (CLI mode)
    if session_id is None:
        session_id = SessionStore.default().load_or_create()
    
    cosmos = await get_cosmos_store()

    if reuse_thread and not thread_id:
        # Load thread_id from Cosmos for this session
        cosmos_tid = await cosmos.get_thread_id(session_id)
        thread_id = cosmos_tid

    global _cached_credential, _cached_client
    if _cached_credential is None:
        _cached_credential = DefaultAzureCredential()
    
    # Cache the client to avoid closing it
    if _cached_client is None:
        _cached_client = await AzureAIAgent.create_client(credential=_cached_credential, endpoint=settings.agent_endpoint).__aenter__()
    
    client = _cached_client
    agent_definition = await _get_or_create_agent_definition(client)
    # Create fresh agent wrapper with cached definition but current client
    agent = AzureAIAgent(client=client, definition=agent_definition, plugins=[HRSearchPlugin()])

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


        #if getattr(thread, "id", None):
        #    span.set_attribute("thread.id", thread.id)

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
                # ✅ Cosmos write only if missing/changed
                if not cosmos_tid or tid != cosmos_tid:
                    await cosmos.upsert_thread_id(session_id, tid)

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            span.set_attribute("agent.elapsed_ms", elapsed_ms)
            answer_text = _to_text(getattr(resp, "content", resp))
            return (answer_text, tid)
        
        # STREAMING MODE - yield chunks as generator
        async def _stream_generator():
            first_token_ms: Optional[int] = None
            
            # Use invoke_stream for true token-level streaming if available
            # Otherwise fall back to invoke which may yield complete messages
            async for msg in agent.invoke_stream(messages=question, thread=thread):
                text = _to_text(getattr(msg, "content", None))
                if text:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - t0) * 1000)
                        span.set_attribute("agent.first_token_ms", first_token_ms)
                    yield {"type": "chunk", "content": text}

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            span.set_attribute("agent.elapsed_ms", elapsed_ms)

            tid = getattr(thread, "id", None)
            span.set_attribute("thread.id", tid or "")
            if reuse_thread and tid:
                if not cosmos_tid or tid != cosmos_tid:
                    await cosmos.upsert_thread_id(session_id, tid)
            
            yield {"type": "done", "thread_id": tid}
        
        return _stream_generator()
