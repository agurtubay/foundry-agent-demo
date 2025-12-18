from __future__ import annotations
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from hr_agent.config import settings

_configured = False

def _get_connection_string_from_project() -> str | None:
    """Get Application Insights connection string from Azure AI Project."""
    try:
        credential = DefaultAzureCredential()
        project_client = AIProjectClient(
            credential=credential,
            endpoint=settings.agent_endpoint
        )
        conn_string = project_client.telemetry.get_application_insights_connection_string()
        credential.close()
        return conn_string
    except Exception as e:
        print(f"Warning: Could not get connection string from AI Project: {e}")
        return None

def setup_telemetry() -> None:
    global _configured
    if _configured:
        return
    
    # Try to get Application Insights connection string from Azure AI Project
    try:
        conn_string = _get_connection_string_from_project()
        if conn_string:
            configure_azure_monitor(connection_string=conn_string)
        else:
            # Fallback to environment variable
            configure_azure_monitor()
    except Exception as e:
        # Fallback to environment variable if project client fails
        print(f"Warning: Failed to setup telemetry from AI Project: {e}")
        configure_azure_monitor()
    
    _configured = True

def get_tracer(name: str = "hr-agent-poc"):
    return trace.get_tracer(name)
