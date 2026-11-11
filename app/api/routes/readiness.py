from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.storage.database import check_database

router = APIRouter()


@router.get("/ready")
async def readiness(request: Request) -> dict[str, str | bool]:
    settings = get_settings()
    try:
        database_ok = await check_database(getattr(request.app.state, "db_engine", None))
    except SQLAlchemyError:
        database_ok = False

    cache = getattr(request.app.state, "assessment_cache", None)
    sqlite_ok = bool(cache is not None and await cache.ping())
    is_ready = database_ok and sqlite_ok
    return {
        "status": "ready" if is_ready else "not_ready",
        "mock": settings.mock,
        "database": "connected" if database_ok else "unavailable",
        "sqlite_cache": "connected" if sqlite_ok else "unavailable",
    }
