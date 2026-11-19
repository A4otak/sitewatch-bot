from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from bot.services.checker import CheckResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class Monitor:
    id: int
    user_id: int
    name: str
    url: str
    last_status: str
    last_code: int | None
    last_latency_ms: int | None
    ssl_days_left: int | None
    ssl_warned: int
    is_active: int
    last_checked_at: str | None
    created_at: str
    check_interval: int | None
    fail_count: int


@dataclass(frozen=True, slots=True)
class Incident:
    id: int
    monitor_id: int
    event_type: str
    details: str
    created_at: str


class Database:
    def __init__(self, path: str):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.path, timeout=10) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    async def initialize(self) -> None:
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    last_status TEXT NOT NULL DEFAULT 'unknown',
                    last_code INTEGER,
                    last_latency_ms INTEGER,
                    ssl_days_left INTEGER,
                    ssl_warned INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    check_interval INTEGER CHECK (check_interval > 0),
                    fail_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = await db.execute_fetchall("PRAGMA table_info(monitors)")
            column_names = {column["name"] for column in columns}
            if "check_interval" not in column_names:
                await db.execute("ALTER TABLE monitors ADD COLUMN check_interval INTEGER")
            if "fail_count" not in column_names:
                await db.execute("ALTER TABLE monitors ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL CHECK (event_type IN ('down', 'up')),
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS incidents_monitor ON incidents(monitor_id, id DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS monitors_user ON monitors(user_id, id)")
            await db.commit()

    async def add(
        self, user_id: int, name: str, url: str, result: CheckResult,
        limit: int, fail_threshold: int, details: str,
    ) -> int | None:
        now = utc_now()
        fail_count = int(result.status == "down")
        status = "unknown" if 0 < fail_count < fail_threshold else result.status
        async with self.connect() as db:
            async with db.execute("""
                INSERT INTO monitors (
                    user_id, name, url, last_status, last_code, last_latency_ms,
                    ssl_days_left, last_checked_at, created_at, fail_count
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE (SELECT count(*) FROM monitors WHERE user_id = ?) < ?
            """, (user_id, name, url, status, result.code, result.latency_ms,
                  result.ssl_days_left, now, now, fail_count, user_id, limit)) as cursor:
                monitor_id = cursor.lastrowid if cursor.rowcount else None
            if monitor_id is not None and status == "down":
                await self._insert_incident(db, monitor_id, "down", details, now)
            await db.commit()
            return monitor_id

    async def list_for_user(self, user_id: int) -> list[Monitor]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT * FROM monitors WHERE user_id = ? ORDER BY id", (user_id,))
        return [Monitor(**dict(row)) for row in rows]

    async def list_active(self) -> list[Monitor]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("SELECT * FROM monitors WHERE is_active = 1 ORDER BY id")
        return [Monitor(**dict(row)) for row in rows]

    async def get(self, monitor_id: int, user_id: int) -> Monitor | None:
        async with self.connect() as db:
            async with db.execute("SELECT * FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)) as cursor:
                row = await cursor.fetchone()
        return Monitor(**dict(row)) if row else None

    async def save_result(
        self, monitor: Monitor, result: CheckResult, fail_threshold: int, details: str,
    ) -> str | None:
        fail_count = monitor.fail_count + 1 if result.status == "down" else 0
        status = monitor.last_status
        event_type = None
        if result.status == "up":
            status = "up"
            if monitor.last_status == "down":
                event_type = "up"
        elif fail_count >= fail_threshold and monitor.last_status != "down":
            status = event_type = "down"

        now = utc_now()
        warned = monitor.ssl_warned if result.ssl_days_left is None or result.ssl_days_left <= 7 else 0
        async with self.connect() as db:
            await db.execute("""
                UPDATE monitors SET last_status = ?, last_code = ?, last_latency_ms = ?,
                    ssl_days_left = ?, ssl_warned = ?, last_checked_at = ?, fail_count = ?
                WHERE id = ? AND user_id = ?
            """, (status, result.code, result.latency_ms, result.ssl_days_left, warned,
                  now, fail_count, monitor.id, monitor.user_id))
            if event_type:
                await self._insert_incident(db, monitor.id, event_type, details, now)
            await db.commit()
        return event_type

    @staticmethod
    async def _insert_incident(
        db: aiosqlite.Connection, monitor_id: int, event_type: str, details: str, created_at: str,
    ) -> None:
        await db.execute(
            "INSERT INTO incidents (monitor_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
            (monitor_id, event_type, details, created_at),
        )

    async def recent_incidents(self, monitor_id: int, user_id: int) -> list[Incident]:
        async with self.connect() as db:
            rows = await db.execute_fetchall("""
                SELECT incidents.* FROM incidents
                JOIN monitors ON monitors.id = incidents.monitor_id
                WHERE incidents.monitor_id = ? AND monitors.user_id = ?
                ORDER BY incidents.id DESC LIMIT 10
            """, (monitor_id, user_id))
        return [Incident(**dict(row)) for row in rows]

    async def mark_ssl_warned(self, monitor: Monitor) -> None:
        await self._change("UPDATE monitors SET ssl_warned = 1 WHERE id = ? AND user_id = ?", monitor.id, monitor.user_id)

    async def toggle(self, monitor_id: int, user_id: int) -> None:
        await self._change("UPDATE monitors SET is_active = 1 - is_active WHERE id = ? AND user_id = ?", monitor_id, user_id)

    async def delete(self, monitor_id: int, user_id: int) -> None:
        await self._change("DELETE FROM monitors WHERE id = ? AND user_id = ?", monitor_id, user_id)

    async def set_interval(self, monitor_id: int, user_id: int, interval: int | None) -> None:
        await self._change("UPDATE monitors SET check_interval = ? WHERE id = ? AND user_id = ?", interval, monitor_id, user_id)

    async def _change(self, sql: str, *args: int | None) -> None:
        async with self.connect() as db:
            await db.execute(sql, args)
            await db.commit()
