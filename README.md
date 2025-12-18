# HR Agent Demo (Azure AI Foundry + Azure AI Search + Cosmos Threads)

Minimal HR Q&A assistant that answers questions grounded in company HR documents (e.g., convenio/BOE) using:

- **Azure AI Foundry Agent Service (classic)** via Semantic Kernel Agents
- **Azure AI Search** for RAG retrieval (chunks index)
- **Azure Cosmos DB** to persist `thread_id` per `session_id` (conversation continuity across restarts)
- **FastAPI + WebSocket UI** (`chat.html`) with a debug panel (raw JSON + timings)
- **Application Insights / Azure Monitor OpenTelemetry** for tracing and latency analysis

---

## Project structure (relevant)

```
src/hr_agent/
  agents/
    hr_agent.py            # main ask() logic
  web/
    chat.html              # frontend UI
  webapp.py                # FastAPI app + websocket endpoint
  config.py                # env vars / settings
  telemetry.py             # Azure Monitor OTEL setup
  cosmos_thread_store.py   # Cosmos read/write for session->thread mapping (AAD)
  session_store.py         # CLI session_id persistence (.state/)
  thread_store.py          # CLI thread_id persistence (.state/)
  cli.py                   # CLI entry (python -m hr_agent.cli)
```

---

## Prerequisites

- Python 3.11+ (you’re on 3.12, perfect)
- Azure resources:
  - Azure AI Foundry Project + Agent (classic)
  - Azure AI Search index with chunked docs
  - Cosmos DB (SQL API) with **Entra ID (AAD) auth** (local auth can be disabled)
  - Application Insights (optional but recommended)

Also:
- `az login` done in the same tenant/subscription where your resources live (for AAD access to Cosmos and optionally Search).

---

## Setup

### 1) Create a virtual environment + install deps

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If you don’t have `requirements.txt`, install at least:

```powershell
pip install semantic-kernel azure-ai-agents azure-identity azure-search-documents azure-cosmos
pip install fastapi uvicorn python-dotenv azure-monitor-opentelemetry
```

---

## Configuration

Create a `.env` in the repo root (never commit it). Required variables:

### Foundry / Agent Service (classic)
- `AZURE_AI_AGENT_ENDPOINT`  
  Example: `https://<your-foundry-agent-endpoint>`
- `AZURE_AI_AGENT_MODEL_DEPLOYMENT_NAME`  
  Example: `gpt-4o-mini` (whatever your Foundry deployment is called)
- `FOUNDRY_AGENT_ID` (optional depending on your implementation)

### Azure AI Search
- `AZURE_SEARCH_ENDPOINT`  
  Example: `https://<search-name>.search.windows.net`
- `AZURE_SEARCH_INDEX`  
  Example: `idx-hr-convenios-chunks`
- `AZURE_SEARCH_API_KEY` (optional)
  - If omitted, your code should use AAD (ensure RBAC is configured)

### Cosmos (thread storage)
- `COSMOS_ENDPOINT`  
  Example: `https://<cosmos-account>.documents.azure.com:443/`
- `COSMOS_DB`  
  Example: `hr-agent`
- `COSMOS_CONTAINER`  
  Example: `sessions`

> Note: This demo assumes Cosmos uses **AAD tokens** (DefaultAzureCredential). If your Cosmos account has local auth disabled, keys will not work.

### Telemetry (optional but recommended)
- `APPLICATIONINSIGHTS_CONNECTION_STRING`

---

## Run (CLI)

CLI is useful for quick local testing:

```powershell
python -m hr_agent.cli "¿Cuántos días de vacaciones tengo?"
python -m hr_agent.cli "Responde otra vez a la pregunta anterior"
```

The CLI prints:
- `run_id`
- `trace_id`
- `thread_id`

It also stores local state in `.state/`:
- `.state/session_id.txt`
- `.state/thread_id.txt`

### Reset conversation (CLI)
Delete `.state/`:

```powershell
Remove-Item -Recurse -Force .\.state
```

---

## Run (Web UI)

Start the FastAPI server:

```powershell
uvicorn hr_agent.webapp:app --reload --port 8000
```

Open:

- http://127.0.0.1:8000

### Web session behavior
- A `session_id` is stored in the browser (localStorage)
- Backend loads `thread_id` for that `session_id` from Cosmos
- Clicking **New chat** clears the stored `session_id` and starts fresh

### Debug panel (right side)
The UI shows:
- session start
- Cosmos get/upsert timings
- request/response JSON payloads
- per-request timings (as returned by the backend)

---

## Observability / Latency analysis

### Where to look
- **Foundry Traces**: see “create_message”, “start_thread_run”, repeated “get_thread_run”, tool calls, etc.
- **Application Insights / Log Analytics (KQL)**: break down dependencies by name and duration.

### Typical latency buckets
- Agent orchestration (polling, runs, step hydration)
- Tool call (Azure AI Search)
- Cosmos thread read/write (should be small; avoid upsert on every turn)

---

## Common issues

### `Missing required env var: ...`
Your `.env` is missing something used by `src/hr_agent/config.py`.

### Cosmos: `(Unauthorized) Local Authorization is disabled. Use an AAD token...`
Your Cosmos account has key-based auth disabled. Ensure you’re using:
- `DefaultAzureCredential()`
- Cosmos DB **data-plane RBAC** role assignment (e.g., Built-in Data Contributor)

### WebSocket sends JSON serialization error
Make sure the backend converts any SDK objects to strings before sending JSON, or uses `jsonable_encoder`.

---

## Next improvements (roadmap)
- Return `trace_id`/`run_id` and per-phase timings directly to the UI
- Add a “benchmark mode” toggle (force new session/thread per run)
- Streaming responses to reduce “run hydration” calls
- Optional “always retrieve first” fast-path to reduce tool-orchestration overhead
