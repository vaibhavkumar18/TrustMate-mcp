# TRUSTMATE MCP Server

TRUSTMATE is Person A's independent seller-intelligence service. It exposes a small MCP interface that the shopping application (Person B) can call to discover offers, assess seller evidence, calculate deterministic trust assessments, rank eligible offers, and explain a trust assessment.

This repository contains the **Python MCP backend only**. It does not contain the customer-facing agent/frontend, PayPal integration, checkout workflow, or dashboard.

## Runtime stack

- Python 3.11+
- FastAPI and Uvicorn
- Official Python MCP SDK v2 over Streamable HTTP
- Pydantic v2 and HTTPX
- SQLAlchemy 2.x async + asyncpg
- Alembic migrations
- Neon PostgreSQL for relational persistence
- SQLite for the 72-hour seller-assessment and explanation cache
- Channel3, Foursquare, OpenStreetMap/Nominatim, URLhaus, RDAP, native website checks, optional Trustpilot Insights reviews, and OpenRouter adapters

## Project layout

```text
trust-mcp/
├── app/
│   ├── api/                 # Health and readiness routes
│   ├── core/                # Settings and logging
│   ├── domain/              # Provider contracts, normalized models, policies
│   ├── mcp/                 # MCP server, schemas, and the three public tools
│   ├── providers/           # Provider registry/factory and adapters
│   ├── services/            # Identity, verification, scoring, ranking, persistence
│   └── storage/             # SQLAlchemy models/sessions and SQLite assessment cache
├── alembic/                 # Database migration history
├── docs/                    # Maintained project documentation
├── tests/                   # Contract, provider, service, evaluation tests
├── .env.example
├── requirements.txt
├── package.json             # Optional Neon CLI tooling
├── package-lock.json
├── neon.ts                  # Optional Neon CLI configuration
└── alembic.ini
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the actual runtime flow and module responsibilities.

## Setup

Use Python 3.11 or later. Render is pinned to Python 3.13.5 through `.python-version`. From the repository root:

### Windows PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

### macOS / Linux

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` locally. Never commit or share it. `.env.example` contains variable names and placeholders only.

## Run the service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Endpoints:

- `GET /health` — process liveness.
- `GET /ready` — readiness; requires working Neon PostgreSQL and SQLite cache connections.
- `POST /mcp` — MCP Streamable HTTP endpoint.

The MCP server exposes exactly three public tools:

1. `rank_offers(query, budget, currency, target_country)`
2. `get_trust_score(seller, url?)`
3. `explain_score(seller, url?)`

The frontend/agent must consume the MCP `structuredContent` response for product cards and must not independently recalculate scores or reorder offers.

## Database

Runtime uses `DATABASE_URL` (Neon's pooled URL when available). Alembic should use `DATABASE_URL_UNPOOLED` for direct migration connections. SQLite is a separate 72-hour cache configured with `SQLITE_PATH` and `CACHE_TTL_DAYS=3`; it does not replace Neon. It caches scored seller-assessment summaries and successful explanations, not raw provider payloads or product listings.

```bash
alembic upgrade head
```

The current migration is `0001_initial_person_a_schema` and creates six Person A tables. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for migration and deployment notes.

## Tests and static checks

```bash
python -m compileall -q app tests
python -m pytest -q
```

Live provider tests are opt-in and should not be run casually against public services. See [docs/PROVIDERS.md](docs/PROVIDERS.md) for provider limitations and live-test controls.

**Current verification note (2026-11-11):** Python bytecode compilation passes. Focused SQLite-cache, cache-policy, seller-orchestration, provider-registry, Trustpilot, OpenRouter-cache-failure and rank-offers cache-integration tests passed (35/35) in the provided environment using a temporary test-only `publicsuffix2` stub because the declared dependency was not installed. The official MCP SDK was unavailable, so the full suite and actual ASGI/MCP startup were not verified here. Do not use the temporary stub in deployment; install `requirements.txt` in a clean environment and run the full suite before production.


## Deploy on Render

This repository includes `render.yaml` for a Render **Web Service** deployment. Push the contents of this `trust-mcp/` project directory as the Git repository root, then create a Blueprint from that repository. Render builds with `pip install -r requirements.txt` and starts `app.main:app` on the injected `$PORT`; Python is pinned to 3.13.5, and the service uses one worker and one instance to preserve the process-local Nominatim rate limit. Do not scale horizontally until that limiter is shared across instances. The Render hostname is wired into `MCP_ALLOWED_HOSTS`, `/health` is the liveness check, and a generated bearer token protects `/mcp`.

During Blueprint creation, provide:

- `CHANNEL3_API_KEY`
- `FOURSQUARE_API_KEY`
- `DATABASE_URL` (your Neon pooled connection string; optional if you deliberately run without persistence)
- `DATABASE_URL_UNPOOLED` for deliberate Alembic migrations (configure when applying migrations)

The Blueprint mounts a 1 GB persistent disk at `/var/data`, with `SQLITE_PATH=/var/data/trustmate.sqlite` and `CACHE_TTL_DAYS=3`. **This requires a paid Render web-service plan (Starter in the Blueprint); Free services cannot attach persistent disks.** Review Render billing before creating/updating the service. Without the disk, SQLite cache contents would be lost on restart or redeploy.

After deployment, copy the generated `MCP_BEARER_TOKEN` from the Render environment settings into the consuming MCP client's Authorization header as `Bearer <token>`. The MCP endpoint is `https://<your-service>.onrender.com/mcp`. Keep the token secret. `/health` stays unauthenticated for Render's health probe; MCP traffic does not.

For the existing Neon database, set `DATABASE_URL_UNPOOLED` when running `alembic upgrade head` as a deliberate release step. Do not run migrations on every web-process startup. `/ready` requires a database; Render is configured to probe `/health` instead.

The Blueprint leaves URLhaus disabled. `REVIEW_PROVIDER` defaults to `none`; Trustpilot reviews stay unavailable until you independently obtain Trustpilot **Insights API** access, then set `REVIEW_PROVIDER=trustpilot` and `TRUSTPILOT_API_KEY` in Render. A Display-only key does not grant the full service-review endpoint.

The included persistent SQLite configuration is for a single paid Render service instance. Render disks are single-instance storage; keep one web instance and one Uvicorn worker. See [Render persistent disk limitations](https://render.com/docs/disks) and [Render Free limitations](https://render.com/docs/free).

## Optional Neon CLI tooling

`package.json`, `package-lock.json`, and `neon.ts` are retained for the existing Neon CLI workflow. The local `.neon` project metadata file is intentionally excluded from the release archive; it is not required to start the Python MCP server. Install the Node dependencies only when using that tooling:

```bash
npm ci
```

`node_modules/` is generated output and is intentionally not part of the source archive.
