"""Restart-safe ownership records for asynchronous mesh jobs."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_VIDEO_JOB_DB = (
    Path.home() / ".local" / "state" / "slancha-mesh" / "router" / "video-jobs.sqlite3"
)
DEFAULT_VIDEO_JOB_TTL_S = 7 * 24 * 60 * 60

_TEXT_LIMITS = {
    "protocol_id": 128,
    "specialist_id": 256,
    "owner_node_id": 256,
    "owner_origin": 2048,
    "upstream_job_id": 1024,
    "status": 64,
}


@dataclass(frozen=True)
class VideoJob:
    public_id: str
    protocol_id: str
    specialist_id: str
    owner_node_id: str
    owner_origin: str
    upstream_job_id: str
    created_at: float
    updated_at: float
    expires_at: float
    status: str


@dataclass(frozen=True)
class VideoDeleteClaim:
    job: VideoJob
    token: str


class VideoJobStore:
    """Small SQLite owner map; each operation owns its connection."""

    def __init__(
        self,
        path: str | Path = DEFAULT_VIDEO_JOB_DB,
        *,
        ttl_s: float = DEFAULT_VIDEO_JOB_TTL_S,
        cleanup_batch: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if cleanup_batch < 1:
            raise ValueError("cleanup_batch must be at least 1")
        self.path = Path(path).expanduser()
        self.ttl_s = float(ttl_s)
        self.cleanup_batch = cleanup_batch
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_jobs (
                    public_id TEXT PRIMARY KEY,
                    protocol_id TEXT NOT NULL,
                    specialist_id TEXT NOT NULL,
                    owner_node_id TEXT NOT NULL,
                    owner_origin TEXT NOT NULL,
                    upstream_job_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_status TEXT NOT NULL,
                    delete_claim_token TEXT,
                    delete_claimed_until REAL,
                    cleanup_pending INTEGER NOT NULL DEFAULT 0,
                    cleanup_reason TEXT
                )
                """
            )
            self._ensure_column(connection, "delete_claim_token", "TEXT")
            self._ensure_column(connection, "delete_claimed_until", "REAL")
            self._ensure_column(
                connection,
                "cleanup_pending",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "cleanup_reason", "TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS video_jobs_expires_at "
                "ON video_jobs(expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS video_jobs_cleanup_pending "
                "ON video_jobs(cleanup_pending, expires_at)"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS video_jobs_cleanup_owner
                ON video_jobs(protocol_id, owner_origin, upstream_job_id)
                WHERE cleanup_pending = 1
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        name: str,
        declaration: str,
    ) -> None:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(video_jobs)")
        }
        if name not in columns:
            try:
                connection.execute(
                    f"ALTER TABLE video_jobs ADD COLUMN {name} {declaration}"
                )
            except sqlite3.OperationalError:
                # Another router process may have completed the idempotent
                # migration after this connection inspected the schema.
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(video_jobs)")
                }
                if name not in columns:
                    raise

    @staticmethod
    def _validate_text(field: str, value: str) -> str:
        limit = _TEXT_LIMITS[field]
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
        return value

    @staticmethod
    def _from_row(row: tuple) -> VideoJob:
        return VideoJob(
            public_id=row[0],
            protocol_id=row[1],
            specialist_id=row[2],
            owner_node_id=row[3],
            owner_origin=row[4],
            upstream_job_id=row[5],
            created_at=row[6],
            updated_at=row[7],
            expires_at=row[8],
            status=row[9],
        )

    def _cleanup_expired(self, connection: sqlite3.Connection, now: float) -> int:
        expired = connection.execute(
            """
            SELECT public_id, protocol_id, specialist_id, owner_node_id,
                   owner_origin, upstream_job_id, created_at, updated_at,
                   expires_at, last_status
            FROM video_jobs
            WHERE expires_at <= ? AND cleanup_pending = 0
            ORDER BY expires_at, public_id
            LIMIT ?
            """,
            (now, self.cleanup_batch),
        ).fetchall()
        for row in expired:
            existing_cleanup = connection.execute(
                """
                SELECT public_id FROM video_jobs
                WHERE protocol_id = ? AND owner_origin = ?
                  AND upstream_job_id = ? AND cleanup_pending = 1
                  AND public_id != ?
                LIMIT 1
                """,
                (row[1], row[4], row[5], row[0]),
            ).fetchone()
            if existing_cleanup is not None:
                connection.execute(
                    """
                    UPDATE video_jobs
                    SET specialist_id = ?, owner_node_id = ?,
                        updated_at = ?, last_status = ?
                    WHERE public_id = ?
                    """,
                    (row[2], row[3], row[7], row[9], existing_cleanup[0]),
                )
                connection.execute(
                    "DELETE FROM video_jobs WHERE public_id = ?",
                    (row[0],),
                )
                continue
            connection.execute(
                """
                UPDATE video_jobs
                SET cleanup_pending = 1, cleanup_reason = 'expired'
                WHERE public_id = ? AND cleanup_pending = 0
                """,
                (row[0],),
            )
        return len(expired)

    def cleanup_expired(self) -> int:
        with self._connect() as connection:
            return self._cleanup_expired(connection, self._clock())

    def create(
        self,
        *,
        protocol_id: str,
        specialist_id: str,
        owner_node_id: str,
        owner_origin: str,
        upstream_job_id: str,
        status: str,
    ) -> VideoJob:
        values = {
            field: self._validate_text(field, value)
            for field, value in {
                "protocol_id": protocol_id,
                "specialist_id": specialist_id,
                "owner_node_id": owner_node_id,
                "owner_origin": owner_origin,
                "upstream_job_id": upstream_job_id,
                "status": status,
            }.items()
        }
        now = self._clock()
        record = VideoJob(
            public_id=f"video_{uuid.uuid4().hex}",
            created_at=now,
            updated_at=now,
            expires_at=now + self.ttl_s,
            **values,
        )
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            connection.execute(
                """
                INSERT INTO video_jobs (
                    public_id, protocol_id, specialist_id, owner_node_id,
                    owner_origin, upstream_job_id, created_at, updated_at,
                    expires_at, last_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.public_id,
                    record.protocol_id,
                    record.specialist_id,
                    record.owner_node_id,
                    record.owner_origin,
                    record.upstream_job_id,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                    record.status,
                ),
            )
        return record

    def enqueue_cleanup(
        self,
        *,
        protocol_id: str,
        specialist_id: str,
        owner_node_id: str,
        owner_origin: str,
        upstream_job_id: str,
        status: str,
    ) -> VideoJob:
        """Persist hidden cleanup work for an upstream job with no public owner."""

        values = {
            field: self._validate_text(field, value)
            for field, value in {
                "protocol_id": protocol_id,
                "specialist_id": specialist_id,
                "owner_node_id": owner_node_id,
                "owner_origin": owner_origin,
                "upstream_job_id": upstream_job_id,
                "status": status,
            }.items()
        }
        now = self._clock()
        record = VideoJob(
            public_id=f"video_{uuid.uuid4().hex}",
            created_at=now,
            updated_at=now,
            expires_at=now,
            **values,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT public_id, protocol_id, specialist_id, owner_node_id,
                       owner_origin, upstream_job_id, created_at, updated_at,
                       expires_at, last_status, cleanup_pending
                FROM video_jobs
                WHERE protocol_id = ? AND owner_origin = ? AND upstream_job_id = ?
                ORDER BY CASE cleanup_pending
                             WHEN 0 THEN 0
                             WHEN 1 THEN 1
                             ELSE 2
                         END,
                         created_at, public_id
                LIMIT 1
                """,
                (
                    record.protocol_id,
                    record.owner_origin,
                    record.upstream_job_id,
                ),
            ).fetchone()
            if existing is not None:
                if existing[10] == 2:
                    connection.execute(
                        """
                        UPDATE video_jobs
                        SET specialist_id = ?, owner_node_id = ?,
                            updated_at = ?, expires_at = ?, last_status = ?,
                            delete_claim_token = NULL,
                            delete_claimed_until = NULL,
                            cleanup_pending = 1,
                            cleanup_reason = 'create_compensation'
                        WHERE public_id = ? AND cleanup_pending = 2
                        """,
                        (
                            record.specialist_id,
                            record.owner_node_id,
                            record.updated_at,
                            record.expires_at,
                            record.status,
                            existing[0],
                        ),
                    )
                    existing = connection.execute(
                        """
                        SELECT public_id, protocol_id, specialist_id,
                               owner_node_id, owner_origin, upstream_job_id,
                               created_at, updated_at, expires_at, last_status
                        FROM video_jobs WHERE public_id = ?
                        """,
                        (existing[0],),
                    ).fetchone()
                return self._from_row(existing)
            connection.execute(
                """
                INSERT INTO video_jobs (
                    public_id, protocol_id, specialist_id, owner_node_id,
                    owner_origin, upstream_job_id, created_at, updated_at,
                    expires_at, last_status, cleanup_pending, cleanup_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'create_compensation')
                """,
                (
                    record.public_id,
                    record.protocol_id,
                    record.specialist_id,
                    record.owner_node_id,
                    record.owner_origin,
                    record.upstream_job_id,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                    record.status,
                ),
            )
        return record

    def get(self, public_id: str) -> VideoJob | None:
        now = self._clock()
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT public_id, protocol_id, specialist_id, owner_node_id,
                       owner_origin, upstream_job_id, created_at, updated_at,
                       expires_at, last_status
                FROM video_jobs
                WHERE public_id = ? AND expires_at > ? AND cleanup_pending = 0
                """,
                (public_id, now),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def update_status(self, public_id: str, status: str) -> VideoJob | None:
        safe_status = self._validate_text("status", status)
        now = self._clock()
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            connection.execute(
                """
                UPDATE video_jobs SET last_status = ?, updated_at = ?
                WHERE public_id = ? AND expires_at > ? AND cleanup_pending = 0
                """,
                (safe_status, now, public_id, now),
            )
        return self.get(public_id)

    def delete(self, public_id: str) -> bool:
        now = self._clock()
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            cursor = connection.execute(
                """DELETE FROM video_jobs
                   WHERE public_id = ? AND expires_at > ? AND cleanup_pending = 0""",
                (public_id, now),
            )
            return cursor.rowcount == 1

    def claim_delete(
        self,
        public_id: str,
        *,
        lease_s: float = 300.0,
    ) -> VideoDeleteClaim | None:
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        now = self._clock()
        token = uuid.uuid4().hex
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            cursor = connection.execute(
                """
                UPDATE video_jobs
                SET delete_claim_token = ?, delete_claimed_until = ?
                WHERE public_id = ? AND expires_at > ?
                  AND cleanup_pending = 0
                  AND (
                    delete_claim_token IS NULL
                    OR delete_claimed_until IS NULL
                    OR delete_claimed_until <= ?
                  )
                """,
                (token, now + lease_s, public_id, now, now),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT public_id, protocol_id, specialist_id, owner_node_id,
                       owner_origin, upstream_job_id, created_at, updated_at,
                       expires_at, last_status
                FROM video_jobs WHERE public_id = ?
                """,
                (public_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - same transaction updated it
            return None
        return VideoDeleteClaim(job=self._from_row(row), token=token)

    def claim_cleanup(
        self,
        *,
        lease_s: float = 300.0,
    ) -> VideoDeleteClaim | None:
        """Lease one hidden cleanup row without dropping its routing owner."""

        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        now = self._clock()
        token = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cleanup_expired(connection, now)
            row = connection.execute(
                """
                SELECT public_id, protocol_id, specialist_id, owner_node_id,
                       owner_origin, upstream_job_id, created_at, updated_at,
                       expires_at, last_status
                FROM video_jobs
                WHERE cleanup_pending = 1
                  AND (
                    delete_claim_token IS NULL
                    OR delete_claimed_until IS NULL
                    OR delete_claimed_until <= ?
                  )
                ORDER BY expires_at, public_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE video_jobs
                SET delete_claim_token = ?, delete_claimed_until = ?
                WHERE public_id = ? AND cleanup_pending = 1
                  AND (
                    delete_claim_token IS NULL
                    OR delete_claimed_until IS NULL
                    OR delete_claimed_until <= ?
                  )
                """,
                (token, now + lease_s, row[0], now),
            )
            if cursor.rowcount != 1:  # pragma: no cover - write txn serializes claims
                return None
        return VideoDeleteClaim(job=self._from_row(row), token=token)

    def release_delete(self, public_id: str, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE video_jobs
                SET delete_claim_token = NULL, delete_claimed_until = NULL
                WHERE public_id = ? AND delete_claim_token = ?
                """,
                (public_id, token),
            )
            return cursor.rowcount == 1

    def quarantine_cleanup(self, public_id: str, token: str) -> bool:
        """Retain malformed cleanup evidence without letting it starve the queue."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE video_jobs
                SET cleanup_pending = 2, cleanup_reason = 'invalid_protocol',
                    delete_claim_token = NULL, delete_claimed_until = NULL
                WHERE public_id = ? AND cleanup_pending = 1
                  AND delete_claim_token = ?
                """,
                (public_id, token),
            )
            return cursor.rowcount == 1

    def confirm_delete(self, public_id: str, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM video_jobs WHERE public_id = ? AND delete_claim_token = ?",
                (public_id, token),
            )
            return cursor.rowcount == 1


__all__ = [
    "DEFAULT_VIDEO_JOB_DB",
    "DEFAULT_VIDEO_JOB_TTL_S",
    "VideoDeleteClaim",
    "VideoJob",
    "VideoJobStore",
]
