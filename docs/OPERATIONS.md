# Operations, Database and Release Checks

## Environment configuration

The complete environment model is defined in `app/core/config.py`; `.env.example` is the editable template. Important groups include:

- Application: `APP_NAME`, `APP_ENV`, `MOCK`, `PORT`, `LOG_LEVEL`.
- PostgreSQL: `DATABASE_URL`, `DATABASE_URL_UNPOOLED`.
- SQLite assessment/explanation cache: `SQLITE_PATH`, `CACHE_TTL_DAYS` (default 3).
- MCP host allowlist: `MCP_ALLOWED_HOSTS`; production bearer token: `MCP_BEARER_TOKEN` (minimum 32 characters).
- Channel3: API key, base URL, timeout and retry settings.
- Foursquare: API key, base URL, API version, timeout/retries and optional Premium setting.
- OSM: base URL, User-Agent, contact email, timeout and cache TTL.
- RDAP: bootstrap URL, timeout and cache TTL.
- Website: timeout and maximum inspected pages.
- Seller reviews: `REVIEW_PROVIDER=none|trustpilot`, Trustpilot Insights API key, base URL, timeout and maximum review pages.
- URLhaus: Auth-Key, base URL, timeout and retries. Keep both authorization flags disabled until intended use and license terms are reviewed; see [PROVIDERS.md](PROVIDERS.md).
- Trustpilot: requires an Insights API entitlement for full service reviews; see [PROVIDERS.md](PROVIDERS.md).
- Seller locality hints: `SELLER_LOCATION_HINTS_JSON` maps a seller's registered domain to a verified physical locality; it is a lookup hint, not identity proof.
- OpenRouter: API key, model, base URL, explainer enablement, timeout and retries.

Never log or share `.env`, API keys, database URLs, or Neon credentials.

## Neon PostgreSQL

- Runtime SQLAlchemy async sessions use `DATABASE_URL` (the pooled Neon URL where available).
- Alembic migrations should use `DATABASE_URL_UNPOOLED` (direct connection) where configured.
- The SQLAlchemy URL is normalized to the `postgresql+asyncpg` dialect in `Settings.sqlalchemy_url()`.
- Six current tables are defined by `app/storage/models/models.py` and created by `alembic/versions/0001_initial_person_a_schema.py`:
  - `sellers`: normalized seller key, display name, registered domain, canonical URL, and identity status.
  - `seller_assessments`: score, label, confidence, assessment status, scoring-policy version, evidence fingerprint, and checked/expiry timestamps.
  - `assessment_factors`: per-factor weight, score, status, detail, and normalized evidence summary.
  - `seller_provider_references`: verified external provider IDs linked to a seller with match confidence.
  - `provider_cache`: provider/capability cache key, result status, normalized payload, provider version, and expiry.
  - `explanation_cache`: evidence-fingerprint-keyed explanation text, LLM provider/model identifiers, and expiry.

Apply migrations from the project root:

```bash
alembic upgrade head
```

Do not reset, drop, or recreate the existing Neon project/database as part of cleanup. Schema changes require a new forward migration; do not edit an already-applied migration in a way that changes deployed history.

## SQLite seller-assessment cache

- SQLite is a cache layer, not a replacement for Neon PostgreSQL.
- `SQLITE_PATH` defaults to `./data/trustmate.sqlite`; `CACHE_TTL_DAYS` defaults to `3`.
- The cache stores application-generated scored seller-assessment summaries and successful explanations for 72 hours by default; it does not store raw provider payloads, page HTML, review text, URLs or credentials. Keys include normalized seller identity, target market, scoring version and provider configuration. Unrated results are not cached so missing evidence can be retried. This derived-output cache is separate from provider-response caching; review source-provider retention terms before public/commercial use.
- The cache uses SQLite WAL mode and creates its parent directory automatically. It is designed for one service instance/one Uvicorn worker.
- On Render, mount a persistent disk at `/var/data` and set `SQLITE_PATH=/var/data/trustmate.sqlite`. Without a persistent disk, SQLite data is lost on restart/redeploy.

## Health and readiness

- `/health` is a liveness check and returns `{"status":"ok"}`.
- `/ready` checks both Neon PostgreSQL and the SQLite cache. It returns `not_ready` if either is unavailable.
- MCP tools are mounted at `/mcp` using the official Python MCP SDK v2 Streamable HTTP transport. Configure the allowed host list for the actual local/deployed hostname.

## Deployment constraints

- The Nominatim rate limiter is process-local. Deploy one worker unless a shared rate limiter is implemented and verified.
- Use HTTPS for the deployed MCP endpoint. Production refuses to start without a 32+ character `MCP_BEARER_TOKEN` and a non-local `MCP_ALLOWED_HOSTS` value. The bearer middleware protects `/mcp` while leaving `/health` available to hosting-platform probes.
- For Render, use the included root `render.yaml`. It pins Python 3.13.5 via `.python-version`, sets `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`, uses `/health`, self-references Render's `RENDER_EXTERNAL_HOSTNAME` for the host allowlist, generates the MCP bearer token, and attaches a 1 GB persistent disk at `/var/data` for SQLite. The Blueprint selects the Starter paid plan because Render persistent disks require a paid web service. Keep one service instance until the Nominatim limiter is shared. Add Channel3, Foursquare and Neon credentials in Render's environment settings; never commit them.
- `DATABASE_URL` is optional at startup, but persistence and `/ready` require a working database. Apply Alembic migrations deliberately as a release step; do not migrate from every web worker's startup.
- Render Free web services cannot attach persistent disks, and their local SQLite files are lost on restarts/redeploys/spin-downs. The included Blueprint uses a paid Starter service to preserve the 72-hour cache across restarts. Review the plan and disk costs in Render before applying the Blueprint.
- Set only the provider credentials required for the selected live mode. Do not enable provider features whose data-use/retention terms have not been reviewed.
- `OPENROUTER_EXPLAINER_ENABLED` defaults to false; explanations remain deterministic fallback unless explicitly enabled.
- URLhaus live lookups remain disabled by default. Keep them disabled until the intended use and applicable provider terms are reviewed; the rest of the trust workflow must treat disabled reputation evidence as unverified (see `PROVIDERS.md`).

## Recommended release checks

1. Create a clean Python 3.13.5 environment (the version pinned in `.python-version`) and install `requirements.txt`.
2. Run `python -m compileall -q app tests`.
3. Run the complete `python -m pytest -q` suite.
4. Run local health/readiness and MCP tool discovery/call smoke tests.
5. Verify each configured provider manually with opt-in/live controls and safe test domains; do not count mocked results as live verification.
6. Verify the Neon migration version and database connectivity without destructive operations.
7. Confirm the three MCP tool names, input schemas, and structured response contract remain unchanged.
8. Deploy and verify the remote `/mcp` endpoint from the consuming application.

## Current verification status

- `python -m compileall -q app tests` passes in the provided container.
- Focused SQLite cache storage/TTL, all-three-tool cache integration, cache-policy, seller-orchestration, provider-registry, Trustpilot and OpenRouter cache-failure tests pass (35/35) using a temporary test-only `publicsuffix2` stub. The real dependency is declared in `requirements.txt`; do not use the test stub in deployment.
- The official `mcp` SDK was unavailable in this sandbox, so the full suite and actual ASGI/MCP startup could not be verified. No live provider or Render/Neon deployment was performed.
- Run the full suite in a clean Python 3.13.5 environment after `pip install -r requirements.txt`, then test provider credentials, SQLite disk persistence, Neon readiness and remote MCP authentication before promoting it.
