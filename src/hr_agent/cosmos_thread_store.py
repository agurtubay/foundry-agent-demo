from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from hr_agent.telemetry import get_tracer

tracer = get_tracer("hr-agent.cosmos")


@dataclass
class CosmosThreadStore:
    endpoint: str
    db_name: str
    container_name: str
    credential: DefaultAzureCredential
    client: CosmosClient

    @staticmethod
    def from_env() -> "CosmosThreadStore":
        return CosmosThreadStore.create_from_env()

    @staticmethod
    def create_from_env() -> "CosmosThreadStore":
        endpoint = os.environ["COSMOS_ENDPOINT"]
        db_name = os.environ["COSMOS_DB"]
        container_name = os.environ["COSMOS_CONTAINER"]

        cred = DefaultAzureCredential()
        client = CosmosClient(endpoint, credential=cred)

        return CosmosThreadStore(
            endpoint=endpoint,
            db_name=db_name,
            container_name=container_name,
            credential=cred,
            client=client,
        )

    def _container(self):
        return self.client.get_database_client(self.db_name).get_container_client(self.container_name)

    async def close(self) -> None:
        await self.client.close()
        await self.credential.close()

    async def get_thread_id(self, session_id: str) -> Optional[str]:
        with tracer.start_as_current_span("cosmos.get_thread_id") as span:
            span.set_attribute("session_id", session_id)
            t0 = time.perf_counter()
            try:
                doc = await self._container().read_item(item=session_id, partition_key=session_id)
                tid = doc.get("thread_id")
                return tid
            except Exception:
                return None
            finally:
                span.set_attribute("cosmos.get_ms", int((time.perf_counter() - t0) * 1000))

    async def upsert_thread_id(self, session_id: str, thread_id: str) -> None:
        with tracer.start_as_current_span("cosmos.upsert_thread_id") as span:
            span.set_attribute("session_id", session_id)
            span.set_attribute("thread_id", thread_id)
            t0 = time.perf_counter()
            try:
                await self._container().upsert_item({
                    "id": session_id,
                    "session_id": session_id,
                    "thread_id": thread_id,
                })
            finally:
                span.set_attribute("cosmos.upsert_ms", int((time.perf_counter() - t0) * 1000))
