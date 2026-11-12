from fastapi import APIRouter, Response

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Health check with JSON response."""
    return {"status": "ok"}


@router.head("/health")
async def health_head() -> Response:
    """Health check without a response body."""
    return Response(status_code=200)
