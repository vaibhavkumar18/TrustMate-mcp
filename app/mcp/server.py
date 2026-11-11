from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.core.config import Settings
from app.mcp.register_tools import register_tools
from app.providers.factory import create_provider_registry


def create_mcp_server(
    settings: Settings,
    provider_registry=None,
    session_factory=None,
    assessment_cache=None,
) -> tuple[MCPServer, object]:
    # Always resolve the registry here, using the explicit settings that were
    # passed in.  This ensures that callers who pass Settings(mock=True) get a
    # mock registry regardless of the lru_cache'd global settings value.
    if provider_registry is None:
        provider_registry = create_provider_registry(settings)
    server = MCPServer(name="trustmate-mcp", version="0.1.0")
    register_tools(server, provider_registry, session_factory, assessment_cache)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.allowed_hosts,
    )
    http_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=transport_security,
        host="0.0.0.0",
    )
    return server, http_app