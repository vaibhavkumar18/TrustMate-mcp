"""SQLite-backed 72-hour cache for completed seller assessments and explanations.

This cache is deliberately separate from Neon PostgreSQL:
- Neon remains the durable relational assessment store.
- SQLite is a small local cache mounted on a Render persistent disk.
- Only scored seller assessments are cached; Unrated results are retried.
- Only successful explanations are cached.
- Raw provider payloads, review snippets, URLs, API keys and credentials are
  never written to this cache.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.mcp.schemas.trust import Trust

logger = logging.getLogger(__name__)


class SQLiteAssessmentCache:
    """Async facade over a small SQLite cache using one connection per operation."""

    def __init__(self, path: str, ttl_days: int = 3) -> None:
        if not path or not isinstance(path, str):
            raise ValueError("SQLITE_PATH must be a non-empty file path")
        if ttl_days < 1:
            raise ValueError("CACHE_TTL_DAYS must be at least 1")
        self.path = path if path == ":memory:" else str(Path(path).expanduser())
        self.ttl_seconds = int(ttl_days) * 24 * 60 * 60
        self.ready = False

    @staticmethod
    def assessment_key(
        seller_key: str,
        target_country: str,
        scoring_version: str,
        provider_profile: dict[str, Any] | None = None,
    ) -> str:
        """Build a stable key that isolates country, policy and provider setup."""
        material = {
            "seller_key": seller_key.strip().casefold(),
            "target_country": (target_country or "GLOBAL").strip().upper(),
            "scoring_version": scoring_version,
            "provider_profile": provider_profile or {},
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self):
        """Commit successful operations and always close the SQLite handle."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)
        self.ready = True

    def _initialize_sync(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS seller_assessment_cache (
                    cache_key TEXT PRIMARY KEY,
                    seller_key TEXT NOT NULL,
                    seller_name TEXT NOT NULL,
                    normalized_domain TEXT,
                    target_country TEXT NOT NULL,
                    scoring_version TEXT NOT NULL,
                    score REAL NOT NULL,
                    label TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    factor_evidence_json TEXT NOT NULL,
                    checked_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_seller_assessment_expiry
                    ON seller_assessment_cache(expires_at);

                CREATE TABLE IF NOT EXISTS explanation_cache (
                    cache_key TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_explanation_cache_expiry
                    ON explanation_cache(expires_at);
                """
            )

    async def ping(self) -> bool:
        try:
            return await asyncio.to_thread(self._ping_sync)
        except sqlite3.Error:
            logger.exception("sqlite_cache_healthcheck_failed")
            return False

    def _ping_sync(self) -> bool:
        with self._connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    async def get_assessment(self, cache_key: str) -> Trust | None:
        try:
            row = await asyncio.to_thread(self._get_assessment_sync, cache_key)
        except sqlite3.Error:
            logger.exception("sqlite_assessment_cache_read_failed")
            return None
        if row is None:
            return None
        try:
            return Trust(
                score=row["score"],
                label=row["label"],
                confidence=row["confidence"],
                factors=json.loads(row["factor_evidence_json"]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "sqlite_assessment_cache_payload_invalid",
                extra={"error_type": type(exc).__name__},
            )
            await self.delete_assessment(cache_key)
            return None

    def _get_assessment_sync(self, cache_key: str) -> sqlite3.Row | None:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT score, label, confidence, factor_evidence_json
                FROM seller_assessment_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, now),
            ).fetchone()
            if row is None:
                connection.execute(
                    "DELETE FROM seller_assessment_cache WHERE cache_key = ? AND expires_at <= ?",
                    (cache_key, now),
                )
            return row

    async def put_assessment(
        self,
        *,
        cache_key: str,
        seller_key: str,
        seller_name: str,
        normalized_domain: str | None,
        target_country: str,
        scoring_version: str,
        trust: Trust,
    ) -> bool:
        # An Unrated result usually means evidence was unavailable or incomplete.
        # Do not freeze that absence for three days; let a later request retry.
        if trust.score is None:
            return False
        factor_json = json.dumps(
            [factor.model_dump(mode="json") for factor in trust.factors],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        now = time.time()
        expires = now + self.ttl_seconds
        try:
            await asyncio.to_thread(
                self._put_assessment_sync,
                cache_key,
                seller_key,
                seller_name,
                normalized_domain,
                target_country,
                scoring_version,
                float(trust.score),
                trust.label,
                trust.confidence,
                factor_json,
                now,
                expires,
            )
            return True
        except sqlite3.Error:
            logger.exception("sqlite_assessment_cache_write_failed")
            return False

    def _put_assessment_sync(
        self,
        cache_key: str,
        seller_key: str,
        seller_name: str,
        normalized_domain: str | None,
        target_country: str,
        scoring_version: str,
        score: float,
        label: str,
        confidence: str,
        factor_json: str,
        checked_at: float,
        expires_at: float,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO seller_assessment_cache (
                    cache_key, seller_key, seller_name, normalized_domain,
                    target_country, scoring_version, score, label, confidence,
                    factor_evidence_json, checked_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    seller_key=excluded.seller_key,
                    seller_name=excluded.seller_name,
                    normalized_domain=excluded.normalized_domain,
                    target_country=excluded.target_country,
                    scoring_version=excluded.scoring_version,
                    score=excluded.score,
                    label=excluded.label,
                    confidence=excluded.confidence,
                    factor_evidence_json=excluded.factor_evidence_json,
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at
                """,
                (
                    cache_key, seller_key, seller_name, normalized_domain,
                    target_country, scoring_version, score, label, confidence,
                    factor_json, checked_at, expires_at,
                ),
            )
            self._purge_expired_sync(connection, checked_at)

    async def delete_assessment(self, cache_key: str) -> None:
        try:
            await asyncio.to_thread(self._delete_assessment_sync, cache_key)
        except sqlite3.Error:
            logger.exception("sqlite_assessment_cache_delete_failed")

    def _delete_assessment_sync(self, cache_key: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM seller_assessment_cache WHERE cache_key = ?", (cache_key,)
            )

    async def get_explanation(self, cache_key: str) -> str | None:
        try:
            return await asyncio.to_thread(self._get_explanation_sync, cache_key)
        except sqlite3.Error:
            logger.exception("sqlite_explanation_cache_read_failed")
            return None

    def _get_explanation_sync(self, cache_key: str) -> str | None:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT summary FROM explanation_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            ).fetchone()
            if row is None:
                connection.execute(
                    "DELETE FROM explanation_cache WHERE cache_key = ? AND expires_at <= ?",
                    (cache_key, now),
                )
                return None
            return str(row["summary"])

    async def put_explanation(self, cache_key: str, summary: str) -> bool:
        if not summary.strip():
            return False
        now = time.time()
        expires = now + self.ttl_seconds
        try:
            await asyncio.to_thread(
                self._put_explanation_sync, cache_key, summary, now, expires
            )
            return True
        except sqlite3.Error:
            logger.exception("sqlite_explanation_cache_write_failed")
            return False

    def _put_explanation_sync(
        self, cache_key: str, summary: str, created_at: float, expires_at: float
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO explanation_cache(cache_key, summary, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    summary=excluded.summary,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
                """,
                (cache_key, summary, created_at, expires_at),
            )
            self._purge_expired_sync(connection, created_at)

    @staticmethod
    def _purge_expired_sync(connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM seller_assessment_cache WHERE expires_at <= ?", (now,)
        )
        connection.execute("DELETE FROM explanation_cache WHERE expires_at <= ?", (now,))

    async def purge_expired(self) -> int:
        try:
            return await asyncio.to_thread(self._purge_expired_count_sync)
        except sqlite3.Error:
            logger.exception("sqlite_cache_cleanup_failed")
            return 0

    def _purge_expired_count_sync(self) -> int:
        now = time.time()
        with self._connection() as connection:
            before = connection.total_changes
            self._purge_expired_sync(connection, now)
            return connection.total_changes - before
