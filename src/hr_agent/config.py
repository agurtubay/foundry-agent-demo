from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

@dataclass(frozen=True)
class Settings:
    # Telemetry
    appinsights_connection_string: str | None = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")

    # Foundry / Agents (classic)
    agent_endpoint: str = _req("AZURE_AI_AGENT_ENDPOINT")
    model_deployment: str = _req("AZURE_AI_AGENT_MODEL_DEPLOYMENT_NAME")
    agent_id: str | None = os.getenv("FOUNDRY_AGENT_ID")

    # Search
    search_endpoint: str = _req("AZURE_SEARCH_ENDPOINT")
    search_index: str = _req("AZURE_SEARCH_INDEX")
    search_api_key: str | None = os.getenv("AZURE_SEARCH_API_KEY")

settings = Settings()
