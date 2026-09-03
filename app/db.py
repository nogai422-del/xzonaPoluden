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
    position_code: str | None = None
    faction_code: str | None = None
    position_status: str = "unassigned"
    approved_by: int | None = None
    approved_at: str | None = None


@dataclass(slots=True)
class RoleRequest:
    id: int
    telegram_id: int
    player_nickname: str
    requested_position_code: str
    requested_faction_code: str | None
    requested_label: str
    status: str
    requested_at: str
    reviewed_by: int | None
    reviewed_at: str | None


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


@dataclass(slots=True)
class MarketOrder:
    id: int
    requester_id: int
    requester_nickname: str
    requester_username: str | None
    comment: str | None
    status: str
    merchant_target: str | None
    delivery_method: str | None
    created_at: str
    sent_at: str | None
    workflow_status: str = "pending"
    topic_chat_id: int | None = None
    topic_thread_id: int | None = None
    topic_message_id: int | None = None
    merchant_message_id: int | None = None


@dataclass(slots=True)
class MarketOrderItem:
    id: int
    order_id: int
    item_name: str
    quantity: int


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

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    requested_position_code TEXT NOT NULL,
                    requested_faction_code TEXT,
                    requested_label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    requested_at TEXT NOT NULL,
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
                    FOREIGN KEY(telegram_id) REFERENCES players(telegram_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_role_requests_status ON role_requests(status, id DESC);
                CREATE INDEX IF NOT EXISTS idx_role_requests_user ON role_requests(telegram_id, id DESC);

                CREATE TABLE IF NOT EXISTS market_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester_id INTEGER NOT NULL,
                    comment TEXT,
                    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created','sent','failed')),
                    merchant_target TEXT,
                    delivery_method TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    FOREIGN KEY(requester_id) REFERENCES players(telegram_id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS market_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    FOREIGN KEY(order_id) REFERENCES market_orders(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_market_orders_requester ON market_orders(requester_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_market_order_items_order ON market_order_items(order_id);
                """
            )
            # Lightweight migrations for installations created by older bot versions.
            cur = await db.execute("PRAGMA table_info(players)")
            player_columns = {row[1] for row in await cur.fetchall()}
            player_migrations = [
                ("position_code", "ALTER TABLE players ADD COLUMN position_code TEXT"),
                ("faction_code", "ALTER TABLE players ADD COLUMN faction_code TEXT"),
                ("position_status", "ALTER TABLE players ADD COLUMN position_status TEXT NOT NULL DEFAULT 'unassigned'"),
                ("approved_by", "ALTER TABLE players ADD COLUMN approved_by INTEGER"),
                ("approved_at", "ALTER TABLE players ADD COLUMN approved_at TEXT"),
            ]
            for column, sql in player_migrations:
                if column not in player_columns:
                    await db.execute(sql)

            cur = await db.execute("PRAGMA table_info(market_orders)")
            market_columns = {row[1] for row in await cur.fetchall()}
            migrations = [
                ("workflow_status", "ALTER TABLE market_orders ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'pending'"),
                ("topic_chat_id", "ALTER TABLE market_orders ADD COLUMN topic_chat_id INTEGER"),
                ("topic_thread_id", "ALTER TABLE market_orders ADD COLUMN topic_thread_id INTEGER"),
                ("topic_message_id", "ALTER TABLE market_orders ADD COLUMN topic_message_id INTEGER"),
                ("merchant_message_id", "ALTER TABLE market_orders ADD COLUMN merchant_message_id INTEGER"),
            ]
            for column, sql in migrations:
                if column not in market_columns:
                    await db.execute(sql)
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

    async def set_setting(self, key: str, value: str) -> None:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO bot_settings(key, value, updated_at)
                VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
            await db.commit()

    async def get_setting(self, key: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM bot_settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def delete_settings(self, keys: list[str]) -> None:
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"DELETE FROM bot_settings WHERE key IN ({placeholders})", keys)
            await db.commit()

    async def set_nicks_topic(self, chat_id: int, thread_id: int) -> None:
        await self.set_setting("nicks_chat_id", str(chat_id))
        await self.set_setting("nicks_thread_id", str(thread_id))

    async def get_nicks_topic(self) -> tuple[int, int] | None:
        chat_id = await self.get_setting("nicks_chat_id")
        thread_id = await self.get_setting("nicks_thread_id")
        if not chat_id or not thread_id:
            return None
        try:
            return int(chat_id), int(thread_id)
        except ValueError:
            return None

    async def set_general_topic(self, chat_id: int, thread_id: int) -> None:
        await self.set_setting("general_chat_id", str(chat_id))
        await self.set_setting("general_thread_id", str(thread_id))

    async def get_general_topic(self) -> tuple[int, int] | None:
        chat_id = await self.get_setting("general_chat_id")
        thread_id = await self.get_setting("general_thread_id")
        if not chat_id or thread_id is None:
            return None
        try:
            return int(chat_id), int(thread_id)
        except ValueError:
            return None

    async def set_storage_topic(self, chat_id: int, thread_id: int) -> None:
        await self.set_setting("storage_chat_id", str(chat_id))
        await self.set_setting("storage_thread_id", str(thread_id))

    async def get_storage_topic(self) -> tuple[int, int] | None:
        chat_id = await self.get_setting("storage_chat_id")
        thread_id = await self.get_setting("storage_thread_id")
        if not chat_id or not thread_id:
            return None
        try:
            return int(chat_id), int(thread_id)
        except ValueError:
            return None

    async def storage_stats(self) -> tuple[int, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT player_id) FROM storage_items WHERE status='stored'"
            )
            row = await cur.fetchone()
            return int(row[0]), int(row[1])

    async def set_market_topic(self, chat_id: int, thread_id: int) -> None:
        await self.set_setting("market_chat_id", str(chat_id))
        await self.set_setting("market_thread_id", str(thread_id))

    async def get_market_topic(self) -> tuple[int, int] | None:
        chat_id = await self.get_setting("market_chat_id")
        thread_id = await self.get_setting("market_thread_id")
        if not chat_id or not thread_id:
            return None
        try:
            return int(chat_id), int(thread_id)
        except ValueError:
            return None

    async def set_nicks_history_imported(self, imported_at: str, imported_count: int) -> None:
        await self.set_setting("nicks_history_imported_at", imported_at)
        await self.set_setting("nicks_history_imported_count", str(imported_count))

    async def get_nicks_history_import_status(self) -> tuple[str | None, int]:
        imported_at = await self.get_setting("nicks_history_imported_at")
        imported_count_raw = await self.get_setting("nicks_history_imported_count")
        try:
            imported_count = int(imported_count_raw or "0")
        except ValueError:
            imported_count = 0
        return imported_at, imported_count

    async def set_telethon_auth(self, api_id: int, api_hash_enc: str, phone: str, session_enc: str) -> None:
        await self.set_setting("telethon_api_id", str(api_id))
        await self.set_setting("telethon_api_hash_enc", api_hash_enc)
        await self.set_setting("telethon_phone", phone)
        await self.set_setting("telethon_session_enc", session_enc)

    async def get_telethon_auth(self) -> dict[str, str] | None:
        api_id = await self.get_setting("telethon_api_id")
        api_hash_enc = await self.get_setting("telethon_api_hash_enc")
        phone = await self.get_setting("telethon_phone")
        session_enc = await self.get_setting("telethon_session_enc")
        if not api_id or not api_hash_enc or not session_enc:
            return None
        return {
            "api_id": api_id,
            "api_hash_enc": api_hash_enc,
            "phone": phone or "",
            "session_enc": session_enc,
        }

    async def clear_telethon_auth(self) -> None:
        await self.delete_settings([
            "telethon_api_id",
            "telethon_api_hash_enc",
            "telethon_phone",
            "telethon_session_enc",
        ])

    async def set_market_merchant_target(self, target: str) -> None:
        await self.set_setting("market_merchant_target", target.strip())

    async def get_market_merchant_target(self) -> str | None:
        return await self.get_setting("market_merchant_target")

    async def set_player_role(
        self,
        telegram_id: int,
        position_code: str,
        faction_code: str | None,
        approved_by: int | None,
    ) -> bool:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                UPDATE players
                SET position_code=?, faction_code=?, position_status='approved',
                    approved_by=?, approved_at=?, updated_at=?
                WHERE telegram_id=?
                """,
                (position_code, faction_code, approved_by, now, now, telegram_id),
            )
            if cur.rowcount > 0:
                await db.execute(
                    """
                    UPDATE role_requests
                    SET status='superseded', reviewed_by=?, reviewed_at=?
                    WHERE telegram_id=? AND status='pending'
                    """,
                    (approved_by, now, telegram_id),
                )
            await db.commit()
            return cur.rowcount > 0

    async def clear_player_role(self, telegram_id: int, approved_by: int | None = None) -> bool:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                UPDATE players
                SET position_code=NULL, faction_code=NULL, position_status='unassigned',
                    approved_by=?, approved_at=?, updated_at=?
                WHERE telegram_id=?
                """,
                (approved_by, now, now, telegram_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def create_role_request(
        self,
        telegram_id: int,
        requested_position_code: str,
        requested_faction_code: str | None,
        requested_label: str,
    ) -> int:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            # Keep one active request per player. If the same request already exists,
            # return it unchanged (important for repeated Telethon history syncs).
            cur = await db.execute(
                """
                SELECT id FROM role_requests
                WHERE telegram_id=? AND status='pending'
                  AND requested_position_code=?
                  AND COALESCE(requested_faction_code, '')=COALESCE(?, '')
                ORDER BY id DESC LIMIT 1
                """,
                (telegram_id, requested_position_code, requested_faction_code),
            )
            existing = await cur.fetchone()
            if existing:
                return int(existing[0])
            await db.execute(
                "UPDATE role_requests SET status='superseded', reviewed_at=? WHERE telegram_id=? AND status='pending'",
                (now, telegram_id),
            )
            cur = await db.execute(
                """
                INSERT INTO role_requests(
                    telegram_id, requested_position_code, requested_faction_code,
                    requested_label, status, requested_at
                ) VALUES(?,?,?,?, 'pending', ?)
                """,
                (telegram_id, requested_position_code, requested_faction_code, requested_label, now),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_role_request(self, request_id: int) -> RoleRequest | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT r.id, r.telegram_id, p.game_nickname AS player_nickname,
                       r.requested_position_code, r.requested_faction_code, r.requested_label,
                       r.status, r.requested_at, r.reviewed_by, r.reviewed_at
                FROM role_requests r
                JOIN players p ON p.telegram_id=r.telegram_id
                WHERE r.id=?
                """,
                (request_id,),
            )
            row = await cur.fetchone()
            return RoleRequest(**dict(row)) if row else None

    async def get_pending_role_request_for_user(self, telegram_id: int) -> RoleRequest | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT r.id, r.telegram_id, p.game_nickname AS player_nickname,
                       r.requested_position_code, r.requested_faction_code, r.requested_label,
                       r.status, r.requested_at, r.reviewed_by, r.reviewed_at
                FROM role_requests r
                JOIN players p ON p.telegram_id=r.telegram_id
                WHERE r.telegram_id=? AND r.status='pending'
                ORDER BY r.id DESC LIMIT 1
                """,
                (telegram_id,),
            )
            row = await cur.fetchone()
            return RoleRequest(**dict(row)) if row else None

    async def list_pending_role_requests(self, limit: int = 30) -> list[RoleRequest]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT r.id, r.telegram_id, p.game_nickname AS player_nickname,
                       r.requested_position_code, r.requested_faction_code, r.requested_label,
                       r.status, r.requested_at, r.reviewed_by, r.reviewed_at
                FROM role_requests r
                JOIN players p ON p.telegram_id=r.telegram_id
                WHERE r.status='pending'
                ORDER BY r.id DESC LIMIT ?
                """,
                (limit,),
            )
            return [RoleRequest(**dict(row)) for row in await cur.fetchall()]

    async def count_pending_role_requests(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM role_requests WHERE status='pending'")
            return int((await cur.fetchone())[0])

    async def review_role_request(self, request_id: int, reviewer_id: int, approve: bool) -> RoleRequest | None:
        request = await self.get_role_request(request_id)
        if not request or request.status != 'pending':
            return None
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE role_requests SET status=?, reviewed_by=?, reviewed_at=? WHERE id=? AND status='pending'",
                ('approved' if approve else 'rejected', reviewer_id, now, request_id),
            )
            if approve:
                await db.execute(
                    """
                    UPDATE players
                    SET position_code=?, faction_code=?, position_status='approved',
                        approved_by=?, approved_at=?, updated_at=?
                    WHERE telegram_id=?
                    """,
                    (request.requested_position_code, request.requested_faction_code, reviewer_id, now, now, request.telegram_id),
                )
            await db.commit()
        return await self.get_role_request(request_id)

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

    async def create_market_order(
        self,
        *,
        requester_id: int,
        items: list[tuple[str, int]],
        comment: str | None,
        merchant_target: str | None,
    ) -> int:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            cur = await db.execute(
                """
                INSERT INTO market_orders(requester_id, comment, status, merchant_target, created_at)
                VALUES(?,?, 'created', ?, ?)
                """,
                (requester_id, comment.strip() if comment else None, merchant_target, now),
            )
            order_id = int(cur.lastrowid)
            await db.executemany(
                "INSERT INTO market_order_items(order_id, item_name, quantity) VALUES(?,?,?)",
                [(order_id, name.strip(), qty) for name, qty in items],
            )
            await db.commit()
            return order_id

    async def get_market_order(self, order_id: int) -> tuple[MarketOrder, list[MarketOrderItem]] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT o.id, o.requester_id, p.game_nickname AS requester_nickname,
                       p.username AS requester_username, o.comment, o.status,
                       o.merchant_target, o.delivery_method, o.created_at, o.sent_at,
                       o.workflow_status, o.topic_chat_id, o.topic_thread_id, o.topic_message_id,
                       o.merchant_message_id
                FROM market_orders o
                JOIN players p ON p.telegram_id=o.requester_id
                WHERE o.id=?
                """,
                (order_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            cur = await db.execute(
                "SELECT id, order_id, item_name, quantity FROM market_order_items WHERE order_id=? ORDER BY id",
                (order_id,),
            )
            item_rows = await cur.fetchall()
            return MarketOrder(**dict(row)), [MarketOrderItem(**dict(r)) for r in item_rows]

    async def list_market_orders(self, requester_id: int | None = None, limit: int = 20) -> list[MarketOrder]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            sql = """
                SELECT o.id, o.requester_id, p.game_nickname AS requester_nickname,
                       p.username AS requester_username, o.comment, o.status,
                       o.merchant_target, o.delivery_method, o.created_at, o.sent_at,
                       o.workflow_status, o.topic_chat_id, o.topic_thread_id, o.topic_message_id,
                       o.merchant_message_id
                FROM market_orders o
                JOIN players p ON p.telegram_id=o.requester_id
            """
            params: list[object] = []
            if requester_id is not None:
                sql += " WHERE o.requester_id=?"
                params.append(requester_id)
            sql += " ORDER BY o.id DESC LIMIT ?"
            params.append(limit)
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
            return [MarketOrder(**dict(r)) for r in rows]

    async def mark_market_order_sent(self, order_id: int, delivery_method: str) -> None:
        now = utc_now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE market_orders SET status='sent', delivery_method=?, sent_at=? WHERE id=?",
                (delivery_method, now, order_id),
            )
            await db.commit()

    async def mark_market_order_failed(self, order_id: int, delivery_method: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE market_orders SET status='failed', delivery_method=? WHERE id=?",
                (delivery_method, order_id),
            )
            await db.commit()

    async def set_market_order_topic_message(
        self, order_id: int, chat_id: int, thread_id: int, message_id: int
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE market_orders SET topic_chat_id=?, topic_thread_id=?, topic_message_id=? WHERE id=?",
                (chat_id, thread_id, message_id, order_id),
            )
            await db.commit()

    async def set_market_order_merchant_message(self, order_id: int, message_id: int | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE market_orders SET merchant_message_id=? WHERE id=?",
                (message_id, order_id),
            )
            await db.commit()

    async def set_market_workflow_status(self, order_id: int, workflow_status: str) -> bool:
        allowed = {"pending", "accepted", "assembled", "issued", "rejected"}
        if workflow_status not in allowed:
            raise ValueError("Unsupported market workflow status")
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE market_orders SET workflow_status=? WHERE id=?",
                (workflow_status, order_id),
            )
            await db.commit()
            return cur.rowcount > 0
