import time
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

            # Run the agent in streaming mode
            t1 = time.perf_counter()
            
            # Generate unique message ID for this response
            import uuid
            message_id = str(uuid.uuid4())
            
            try:
                # Send stream start
                await ws.send_json(jsonable_encoder({
                    "type": "stream_start",
                    "message_id": message_id,
                    "session_id": session_id
                }))
                
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
                
                async for chunk in stream_generator:
                    if chunk["type"] == "chunk":
                        content = chunk["content"]
                        accumulated_text.append(content)
                        
                        # Send chunk to client
                        await ws.send_json(jsonable_encoder({
                            "type": "stream_chunk",
                            "message_id": message_id,
                            "content": content
                        }))
                        
                    elif chunk["type"] == "done":
                        new_tid = chunk.get("thread_id")
                
                agent_ms = int((time.perf_counter() - t1) * 1000)
                
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
                "answer": "".join(accumulated_text),
                "agent": "hr_agent",
                "session_id": session_id,
                "thread_id": thread_id,
                "timings_ms": {"agent_total_ms": agent_ms}
            })

    except WebSocketDisconnect:
        # Client closed
        return
