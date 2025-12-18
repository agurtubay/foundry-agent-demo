from __future__ import annotations
from typing import List, Dict

from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient  # 

from hr_agent.config import settings
from hr_agent.telemetry import get_tracer

tracer = get_tracer("hr-agent.search")

def _client() -> SearchClient:
    if settings.search_api_key:
        cred = AzureKeyCredential(settings.search_api_key)
    else:
        cred = DefaultAzureCredential()
    return SearchClient(endpoint=settings.search_endpoint, index_name=settings.search_index, credential=cred)

def search_hr_chunks(query: str, top: int = 3) -> List[Dict]:
    """
    Returns top chunk-documents from Azure AI Search for the given query.
    """
    with tracer.start_as_current_span("tool.search_hr_chunks") as span:
        span.set_attribute("search.query", query)
        span.set_attribute("search.top", top)

        client = _client()
        results = client.search(
            search_text=query,
            top=top,
            select=["chunk_id", "parent_id", "chunk", "metadata_storage_name", "metadata_storage_path"],
        )

        rows: List[Dict] = []
        for r in results:
            rows.append({
                "chunk_id": r.get("chunk_id"),
                "parent_id": r.get("parent_id"),
                "chunk": r.get("chunk"),
                "file": r.get("metadata_storage_name"),
                "path": r.get("metadata_storage_path"),
            })

        span.set_attribute("search.returned", len(rows))
        return rows
