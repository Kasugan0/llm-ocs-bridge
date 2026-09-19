from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

import aiosqlite

from .config import settings

_PRUNABLE_TABLES = {
    "search_cache": "created_at",
    "answer_cache": "created_at",
    "error_log": "timestamp",
}


class PostCommitError(RuntimeError):
    pass


class AsyncSQLiteStore:
    _write_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser().resolve())
        self._conn: aiosqlite.Connection | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = self._write_locks.setdefault(self.path, asyncio.Lock())

    async def connect(self) -> None:
        if self._conn is not None:
            return
        async with self._connect_lock:
            if self._conn is not None:
                return
            async with self._write_lock:
                self._prepare_database_file()
                conn = await aiosqlite.connect(
                    self.path,
                    timeout=settings.sqlite_busy_timeout_ms / 1000,
                )
                try:
                    conn.row_factory = aiosqlite.Row
                    await conn.execute(
                        f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}"
                    )
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                    await conn.execute("PRAGMA foreign_keys=ON")
                    await self._init_db(conn)
                    self._secure_file_permissions()
                    await self._enforce_capacity_locked(conn)
                except BaseException:
                    await conn.close()
                    raise
                self._conn = conn

    async def close(self) -> None:
        async with self._connect_lock, self._write_lock:
            if self._conn is None:
                return
            await self._conn.close()
            self._conn = None
            self._secure_file_permissions()

    async def _db(self) -> aiosqlite.Connection:
        await self.connect()
        assert self._conn is not None
        return self._conn

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await self._db()
        async with self._write_lock:
            await self._enforce_capacity_locked(db)
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            try:
                self._secure_file_permissions()
                await self._enforce_capacity_locked(db)
            except Exception as exc:
                raise PostCommitError(
                    "Write committed; SQLite maintenance failed"
                ) from exc

    def database_size_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self._database_files() if path.exists()
        )

    async def _enforce_capacity_locked(self, db: aiosqlite.Connection) -> None:
        if self.database_size_bytes() <= settings.max_cache_bytes:
            return
        await self._checkpoint(db)
        if self.database_size_bytes() <= settings.max_cache_bytes:
            return
        while await self._live_size_bytes(db) > settings.max_cache_bytes:
            rows = await self._oldest_rows(db)
            if not rows:
                raise RuntimeError("SQLite database exceeds MAX_CACHE_BYTES")
            try:
                for table, row_ids in self._group_rows(rows).items():
                    placeholders = ",".join("?" for _ in row_ids)
                    async with db.execute(
                        f"DELETE FROM {table} WHERE rowid IN ({placeholders})", row_ids
                    ):
                        pass
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        async with db.execute("VACUUM"):
            pass
        await self._checkpoint(db)
        self._secure_file_permissions()
        if self.database_size_bytes() > settings.max_cache_bytes:
            raise RuntimeError("SQLite database exceeds MAX_CACHE_BYTES")

    async def _checkpoint(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
            result = await cursor.fetchone()
        if result[0]:
            raise RuntimeError("SQLite checkpoint blocked by another connection")

    async def _live_size_bytes(self, db: aiosqlite.Connection) -> int:
        async with db.execute(
            "SELECT (page_count - freelist_count) * page_size "
            "FROM pragma_page_count(), pragma_freelist_count(), pragma_page_size()"
        ) as cursor:
            row = await cursor.fetchone()
        shm = self._database_files()[2]
        return int(row[0]) + (shm.stat().st_size if shm.exists() else 0)

    async def _oldest_rows(
        self,
        db: aiosqlite.Connection,
    ) -> list[tuple[int, str, int]]:
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        await cursor.close()
        rows: list[tuple[int, str, int]] = []
        for table, timestamp_column in _PRUNABLE_TABLES.items():
            if table not in existing:
                continue
            cursor = await db.execute(
                f"""
                SELECT rowid, {timestamp_column} AS created_at
                FROM {table}
                ORDER BY {timestamp_column}, rowid
                LIMIT ?
                """,
                (settings.cache_prune_batch_size,),
            )
            rows.extend(
                (int(row["created_at"]), table, int(row["rowid"]))
                for row in await cursor.fetchall()
            )
            await cursor.close()
        return sorted(rows)[: settings.cache_prune_batch_size]

    @staticmethod
    def _group_rows(rows: list[tuple[int, str, int]]) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = {}
        for _, table, row_id in rows:
            grouped.setdefault(table, []).append(row_id)
        return grouped

    def _database_files(self) -> tuple[Path, Path, Path]:
        path = Path(self.path)
        return path, Path(f"{path}-wal"), Path(f"{path}-shm")

    def _prepare_database_file(self) -> None:
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self._secure_file_permissions()

    def _secure_file_permissions(self) -> None:
        for path in self._database_files():
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except OSError as exc:
                    raise RuntimeError(
                        f"Cannot secure SQLite file permissions: {path}"
                    ) from exc

    async def _init_db(self, db: aiosqlite.Connection) -> None:
        raise NotImplementedError
