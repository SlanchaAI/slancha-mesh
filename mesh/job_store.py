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
                    last_status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS video_jobs_expires_at "
                "ON video_jobs(expires_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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
        cursor = connection.execute(
            """
            DELETE FROM video_jobs
            WHERE public_id IN (
                SELECT public_id FROM video_jobs
                WHERE expires_at <= ?
                ORDER BY expires_at
                LIMIT ?
            )
            """,
            (now, self.cleanup_batch),
        )
        return cursor.rowcount

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
                WHERE public_id = ? AND expires_at > ?
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
                WHERE public_id = ? AND expires_at > ?
                """,
                (safe_status, now, public_id, now),
            )
        return self.get(public_id)

    def delete(self, public_id: str) -> bool:
        now = self._clock()
        with self._connect() as connection:
            self._cleanup_expired(connection, now)
            cursor = connection.execute(
                "DELETE FROM video_jobs WHERE public_id = ? AND expires_at > ?",
                (public_id, now),
            )
            return cursor.rowcount == 1


__all__ = [
    "DEFAULT_VIDEO_JOB_DB",
    "DEFAULT_VIDEO_JOB_TTL_S",
    "VideoJob",
    "VideoJobStore",
]
