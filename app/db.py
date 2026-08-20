from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Player:
    telegram_id: int
    username: str | None
    full_name: str
    game_nickname: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class StorageItem:
    id: int
    player_id: int
    player_nickname: str
    item_name: str
    quantity: int
    comment: str | None
    status: str
    accepted_by: int
    accepted_at: str
    issued_by: int | None
    issued_at: str | None


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    game_nickname TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS item_names (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    use_count INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    comment TEXT,
                    status TEXT NOT NULL DEFAULT 'stored' CHECK (status IN ('stored','issued')),
                    accepted_by INTEGER NOT NULL,
                    accepted_at TEXT NOT NULL,
                    issued_by INTEGER,
                    issued_at TEXT,
                    FOREIGN KEY(player_id) REFERENCES players(telegram_id) ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_storage_status ON storage_items(status);
                CREATE INDEX IF NOT EXISTS idx_storage_player ON storage_items(player_id);
                CREATE INDEX IF NOT EXISTS idx_item_names_recent ON item_names(last_used_at DESC);
                """
            )
            await db.commit()

    async def upsert_player(
        self, telegram_id: int, username: str | None, full_name: str, game_nickname: str
    ) -> None:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO players(telegram_id, username, full_name, game_nickname, created_at, updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    game_nickname=excluded.game_nickname,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, username, full_name, game_nickname.strip(), now, now),
            )
            await db.commit()

    async def nickname_exists_for_other(self, telegram_id: int, nickname: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM players WHERE game_nickname = ? COLLATE NOCASE AND telegram_id != ? LIMIT 1",
                (nickname.strip(), telegram_id),
            )
            return await cur.fetchone() is not None

    async def get_player(self, telegram_id: int) -> Player | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM players WHERE telegram_id = ?", (telegram_id,))
            row = await cur.fetchone()
            return Player(**dict(row)) if row else None

    async def list_players(self, limit: int = 100, offset: int = 0) -> list[Player]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM players ORDER BY game_nickname COLLATE NOCASE LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cur.fetchall()
            return [Player(**dict(r)) for r in rows]

    async def count_players(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM players")
            return int((await cur.fetchone())[0])

    async def remember_item_name(self, name: str) -> None:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO item_names(name, use_count, last_used_at)
                VALUES(?,1,?)
                ON CONFLICT(name) DO UPDATE SET
                    use_count=use_count+1,
                    last_used_at=excluded.last_used_at
                """,
                (name.strip(), now),
            )
            await db.commit()

    async def recent_item_names(self, limit: int = 8) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name FROM item_names ORDER BY last_used_at DESC, use_count DESC LIMIT ?",
                (limit,),
            )
            return [r[0] for r in await cur.fetchall()]

    async def add_storage_item(
        self,
        *,
        player_id: int,
        item_name: str,
        quantity: int,
        comment: str | None,
        accepted_by: int,
    ) -> int:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                INSERT INTO storage_items(player_id, item_name, quantity, comment, status, accepted_by, accepted_at)
                VALUES(?,?,?,?, 'stored', ?, ?)
                """,
                (player_id, item_name.strip(), quantity, comment.strip() if comment else None, accepted_by, now),
            )
            await db.commit()
            item_id = int(cur.lastrowid)
        await self.remember_item_name(item_name)
        return item_id

    async def get_storage_item(self, item_id: int) -> StorageItem | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.player_id, p.game_nickname AS player_nickname,
                       s.item_name, s.quantity, s.comment, s.status,
                       s.accepted_by, s.accepted_at, s.issued_by, s.issued_at
                FROM storage_items s
                JOIN players p ON p.telegram_id=s.player_id
                WHERE s.id=?
                """,
                (item_id,),
            )
            row = await cur.fetchone()
            return StorageItem(**dict(row)) if row else None

    async def list_storage_items(self, status: str = "stored", limit: int = 20, offset: int = 0) -> list[StorageItem]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.player_id, p.game_nickname AS player_nickname,
                       s.item_name, s.quantity, s.comment, s.status,
                       s.accepted_by, s.accepted_at, s.issued_by, s.issued_at
                FROM storage_items s
                JOIN players p ON p.telegram_id=s.player_id
                WHERE s.status=?
                ORDER BY s.id DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            )
            rows = await cur.fetchall()
            return [StorageItem(**dict(r)) for r in rows]

    async def list_player_items(self, player_id: int, status: str = "stored", limit: int = 30) -> list[StorageItem]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT s.id, s.player_id, p.game_nickname AS player_nickname,
                       s.item_name, s.quantity, s.comment, s.status,
                       s.accepted_by, s.accepted_at, s.issued_by, s.issued_at
                FROM storage_items s
                JOIN players p ON p.telegram_id=s.player_id
                WHERE s.player_id=? AND s.status=?
                ORDER BY s.id DESC LIMIT ?
                """,
                (player_id, status, limit),
            )
            rows = await cur.fetchall()
            return [StorageItem(**dict(r)) for r in rows]

    async def issue_item(self, item_id: int, issued_by: int) -> bool:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                UPDATE storage_items
                SET status='issued', issued_by=?, issued_at=?
                WHERE id=? AND status='stored'
                """,
                (issued_by, now, item_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def delete_item(self, item_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("DELETE FROM storage_items WHERE id=? AND status='stored'", (item_id,))
            await db.commit()
            return cur.rowcount > 0

    async def update_item_name(self, item_id: int, name: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("UPDATE storage_items SET item_name=? WHERE id=? AND status='stored'", (name.strip(), item_id))
            await db.commit()
            ok = cur.rowcount > 0
        if ok:
            await self.remember_item_name(name)
        return ok

    async def update_item_quantity(self, item_id: int, quantity: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("UPDATE storage_items SET quantity=? WHERE id=? AND status='stored'", (quantity, item_id))
            await db.commit()
            return cur.rowcount > 0

    async def update_item_comment(self, item_id: int, comment: str | None) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE storage_items SET comment=? WHERE id=? AND status='stored'",
                (comment.strip() if comment else None, item_id),
            )
            await db.commit()
            return cur.rowcount > 0
