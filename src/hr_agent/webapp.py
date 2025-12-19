import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional, cast, AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.encoders import jsonable_encoder


from hr_agent.agents.hr_agent import ask
from hr_agent.telemetry import setup_telemetry
from hr_agent.cosmos_thread_store import CosmosThreadStore

app = FastAPI()

# Singleton Cosmos store (AAD / DefaultAzureCredential version you already use)
cosmos: CosmosThreadStore | None = None

@app.on_event("startup")
async def _startup():
    global cosmos
    setup_telemetry()
    cosmos = CosmosThreadStore.create_from_env()

@app.on_event("shutdown")
async def _shutdown():
    global cosmos
    if cosmos is not None:
        await cosmos.close()
        cosmos = None


CHAT_HTML_PATH = Path(__file__).parent / "web" / "chat.html"


@app.get("/")
async def index():
    return HTMLResponse(CHAT_HTML_PATH.read_text(encoding="utf-8"))


async def send_debug(ws: WebSocket, log_type: str, message: str, data=None):
    payload = {
        "type": "debug_log",
        "log_type": log_type,
        "message": message,
        "data": data or {},
    }
    await ws.send_json(jsonable_encoder(payload))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    assert cosmos is not None, "Cosmos store not initialized"

    session_id = ws.query_params.get("session_id") or "session_unknown"
    await send_debug(ws, "info", "Session started", {"session_id": session_id})

    # Load thread_id once per websocket connection
    t0 = time.perf_counter()
    ttfc_ms = None
    thread_id: Optional[str] = await cosmos.get_thread_id(session_id)
    await send_debug(
        ws,
        "info",
        "Cosmos get_thread_id",
        {
            "session_id": session_id,
            "thread_id": thread_id,
            "ms": int((time.perf_counter() - t0) * 1000),
        },
    )

    try:
        while True:
            payload = await ws.receive_json()
            user_msg = payload.get("message", "")

            await send_debug(ws, "outgoing", "Client message", {"message": user_msg})

            # Respect client streaming preference (payload.streaming: bool)
            streaming = bool(payload.get("streaming", True))
            t1 = time.perf_counter()

            # Generate unique message ID for this response
            
            message_id = str(uuid.uuid4())

            answer_text = ""
            new_tid = None
            agent_ms = 0

            if streaming:
                try:
                    # Send stream start
                    await ws.send_json(jsonable_encoder({
                        "type": "stream_start",
                        "message_id": message_id,
                        "session_id": session_id
                    }))

                    # Background task to show "Still working..." after 3 seconds
                    async def _show_progress():
                        await asyncio.sleep(3.0)
                        await ws.send_json(jsonable_encoder({
                            "type": "status",
                            "message_id": message_id,
                            "status": "Still working..."
                        }))

                    progress_task = asyncio.create_task(_show_progress())

                    stream_result = await ask(
                        user_msg,
                        thread_id=thread_id,
                        reuse_thread=True,
                        stream=True,
                        session_id=session_id,
                    )
                    stream_generator = cast(AsyncGenerator[dict, None], stream_result)

                    accumulated_text = []
                    new_tid = None
                    first_chunk = True

                    async for chunk in stream_generator:
                        if chunk["type"] == "chunk":
                            content = chunk["content"]
                            accumulated_text.append(content)

                            # Cancel progress task on first chunk
                            if first_chunk:
                                progress_task.cancel()
                                first_chunk = False

                                ttfc_ms = int((time.perf_counter() - t1) * 1000)

                                await send_debug(ws, "incoming", "Stream First Char", {
                                    "message_id": message_id,
                                    "session_id": session_id,
                                    "thread_id": thread_id,
                                    "ttfc_ms": ttfc_ms
                                })

                            # Send chunk to client
                            await ws.send_json(jsonable_encoder({
                                "type": "stream_chunk",
                                "message_id": message_id,
                                "content": content
                            }))

                        elif chunk["type"] == "done":
                            new_tid = chunk.get("thread_id")

                    agent_ms = int((time.perf_counter() - t1) * 1000)

                    # Clean up progress task
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

                    answer_text = "".join(accumulated_text)

                    # Send stream end with metadata
                    await ws.send_json(jsonable_encoder({
                        "type": "stream_end",
                        "message_id": message_id,
                        "thread_id": new_tid,
                        "timings_ms": {"agent_total_ms": agent_ms}
                    }))

                except Exception as e:
                    # Send error message to client on stream failure
                    await send_debug(ws, "error", "Stream error", {"error": str(e), "message_id": message_id})
                    await ws.send_json(jsonable_encoder({
                        "type": "stream_error",
                        "message_id": message_id,
                        "error": str(e)
                    }))
                    continue

            else:
                # Non-streaming: call agent and return a single response
                try:
                    resp_text, new_tid = await ask(
                        user_msg,
                        thread_id=thread_id,
                        reuse_thread=True,
                        stream=False,
                        session_id=session_id,
                    )
                    answer_text = resp_text
                    agent_ms = int((time.perf_counter() - t1) * 1000)

                    # Send a single response payload (legacy/non-streaming client)
                    await ws.send_json(jsonable_encoder({
                        "answer": resp_text,
                        "agent": "hr_agent",
                        "message_id": message_id,
                        "thread_id": new_tid,
                        "timings_ms": {"agent_total_ms": agent_ms}
                    }))

                except Exception as e:
                    await send_debug(ws, "error", "Non-stream error", {"error": str(e), "message_id": message_id})
                    await ws.send_json(jsonable_encoder({
                        "type": "stream_error",
                        "message_id": message_id,
                        "error": str(e)
                    }))
                    continue

            # Persist thread only if missing/changed
            if new_tid and new_tid != thread_id:
                t2 = time.perf_counter()
                await cosmos.upsert_thread_id(session_id, new_tid)
                await send_debug(
                    ws,
                    "info",
                    "Cosmos upsert_thread_id",
                    {
                        "session_id": session_id,
                        "thread_id": new_tid,
                        "ms": int((time.perf_counter() - t2) * 1000),
                    },
                )
                thread_id = new_tid
            
            # Send debug info with complete answer
            await send_debug(ws, "incoming", "Server response complete", {
                "message_id": message_id,
                "answer": answer_text,
                "agent": "hr_agent",
                "session_id": session_id,
                "thread_id": thread_id,
                "timings_ms": {"agent_total_ms": agent_ms}
            })

    except WebSocketDisconnect:
        # Client closed
        return
