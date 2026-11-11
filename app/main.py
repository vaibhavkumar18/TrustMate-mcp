from contextlib import asynccontextmanager
import hmac
import json
import logging

from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.readiness import router as readiness_router
from app.core.config import get_settings
from app.core.logging_setup import configure_logging
from app.mcp.server import create_mcp_server
from app.providers.factory import create_provider_registry
from app.storage.database import create_engine, create_session_factory
from app.storage.sqlite_assessment_cache import SQLiteAssessmentCache


class MCPBearerAuthMiddleware:
    """Protect MCP traffic while leaving platform health probes public."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                supplied = b""
                for key, value in scope.get("headers", []):
                    if key.lower() == b"authorization":
                        supplied = value
                        break
                if not hmac.compare_digest(supplied, self._expected):
                    body = json.dumps({"detail": "Unauthorized"}).encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"www-authenticate", b"Bearer"),
                            (b"cache-control", b"no-store"),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.app_env.lower() == "production":
        if not settings.mcp_bearer_token or len(settings.mcp_bearer_token) < 32:
            raise RuntimeError("MCP_BEARER_TOKEN (at least 32 characters) is required in production")
        if not settings.allowed_hosts or all(
            host in {"localhost:8000", "127.0.0.1:8000"} for host in settings.allowed_hosts
        ):
            raise RuntimeError("MCP_ALLOWED_HOSTS must include the deployed public hostname in production")
    provider_registry = create_provider_registry(settings)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine) if engine else None
    assessment_cache = SQLiteAssessmentCache(
        settings.sqlite_path, ttl_days=settings.cache_ttl_days
    )
    mcp_server, mcp_app = create_mcp_server(
        settings, provider_registry, session_factory, assessment_cache
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.db_engine = engine
        application.state.db_session_factory = session_factory
        application.state.providers = provider_registry
        application.state.assessment_cache = assessment_cache
        await assessment_cache.initialize()
        logger = logging.getLogger(__name__)
        logger.info(
            "sqlite_assessment_cache_initialized",
            extra={"cache_path": settings.sqlite_path, "cache_ttl_days": settings.cache_ttl_days},
        )
        await assessment_cache.purge_expired()
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            if engine:
                await engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(readiness_router)
    app.mount("/", mcp_app)
    if settings.app_env.lower() == "production":
        app.add_middleware(MCPBearerAuthMiddleware, token=settings.mcp_bearer_token or "")
    return app


app = create_app()