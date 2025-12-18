from __future__ import annotations
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

_configured = False

def setup_telemetry() -> None:
    global _configured
    if _configured:
        return
    # Uses APPLICATIONINSIGHTS_CONNECTION_STRING env var if present. 
    configure_azure_monitor()
    _configured = True

def get_tracer(name: str = "hr-agent-poc"):
    return trace.get_tracer(name)
