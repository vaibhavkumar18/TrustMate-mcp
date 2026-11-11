import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.mcp.server import create_mcp_server


def test_health_and_readiness():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        readiness = client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "ready"
        assert isinstance(readiness.json()["mock"], bool)
        assert readiness.json()["database"] == "connected"


@pytest.mark.asyncio
async def test_exactly_three_tools_and_mock_calls():
    from app.core.config import Settings
    mock_settings = Settings(mock=True)
    server, _ = create_mcp_server(mock_settings)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "rank_offers",
        "get_trust_score",
        "explain_score",
    }

    ranked = await server.call_tool(
        "rank_offers", {
            "query": "laptop for programming",
            "budget": 60000,
            "currency": "USD",
            "target_country": "US",
        }
    )
    assert len(ranked.structured_content["offers"]) == 4
    assert ranked.structured_content["offers"][0]["recommended"] is True
    assert "price" in ranked.structured_content["offers"][0]
    assert "currency" in ranked.structured_content["offers"][0]
    assert "price_inr" not in ranked.structured_content["offers"][0]

    trust = await server.call_tool(
        "get_trust_score", {"seller": "Example Store", "url": "https://example.com"}
    )
    assert trust.structured_content["score"] is not None
    assert trust.structured_content["label"] == "Trusted"

    explanation = await server.call_tool(
        "explain_score", {"seller": "Example Store", "url": "https://example.com"}
    )
    assert "summary" in explanation.structured_content
    assert "Trusted" in explanation.structured_content["summary"]
