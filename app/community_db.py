"""Shared catalogue, warehouse approvals, classifieds and faction relations.

All stock/status transitions run under a SQLite write transaction. Telegram
delivery is separate and retryable: a failed send never rolls back a decision.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone

import aiosqlite

from .roles import FACTIONS, is_external_position


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def name_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def clean_name(value: str) -> str:
    value = " ".join(value.split())
    if not 1 <= len(value) <= 80 or value.startswith("/"):
        raise ValueError("Название должно содержать от 1 до 80 символов.")
    return value


async def rows(conn, sql, args=()):
    cur = await conn.execute(sql, args)
    return [dict(r) for r in await cur.fetchall()]


async def require_role(conn, user_id, allowed=None):
    result = await rows(conn, "SELECT * FROM players WHERE telegram_id=?", (user_id,))
    p = result[0] if result else None
    if (not p or p["position_status"] != "approved" or not p["position_code"]
            or is_external_position(p["position_code"])
            or (allowed is not None and p["position_code"] not in allowed)):
        raise ValueError("Для этого действия нужна подтверждённая должность.")
    return p


async def audit(conn, actor, action, details):
    await conn.execute("INSERT INTO audit_log(actor_id,action,details,created_at) VALUES(?,?,?,?)",
                       (actor, action, str(details), now()))


class CommunityDatabase:
    async def get_warehouse_topic(self):
        # gp_stock is the communal inventory shown in the user's forum;
        # storage historically held player deposits. Fall back for installations
        # that configured only a single storage topic.
        return await self.get_topic('gp_stock') or await self.get_topic('storage')

    async def community_init(self):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS trader_orders (
                    order_id INTEGER PRIMARY KEY REFERENCES market_orders(id)
                );
                CREATE TABLE IF NOT EXISTS catalogue (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
                    archived INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS warehouse_requests (
                    id INTEGER PRIMARY KEY, requester_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES catalogue(id),
                    quantity INTEGER NOT NULL CHECK(quantity > 0), reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','issued','rejected','cancelled')),
                    reviewed_by INTEGER, issued_by INTEGER, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS sale_ads (
                    id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL REFERENCES catalogue(id), photo_id TEXT NOT NULL,
                    price INTEGER NOT NULL CHECK(price > 0), description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sold','removed')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, request_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS factions (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS faction_relations (
                    a INTEGER NOT NULL REFERENCES factions(id), b INTEGER NOT NULL REFERENCES factions(id),
                    relation TEXT NOT NULL CHECK(relation IN ('ally','neutral','war')),
                    updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(a,b), CHECK(a < b)
                );
                CREATE TABLE IF NOT EXISTS ally_target_decisions (
                    faction_id INTEGER PRIMARY KEY, basis TEXT NOT NULL,
                    approved INTEGER NOT NULL, actor_id INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS faction_targets (
                    faction_id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL UNIQUE REFERENCES targets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_warehouse_state ON warehouse_requests(status,id);
                CREATE INDEX IF NOT EXISTS idx_sale_state ON sale_ads(status,id);
            """)
            # One-time import: retain player deposits and old orders as history.
            marker = await rows(conn, "SELECT value FROM bot_settings WHERE key='community_migration_v1'")
            if not marker:
                await conn.execute("BEGIN IMMEDIATE")
                for label in ["Полдень", *FACTIONS.values(), "Одиночки"]:
                    await conn.execute("INSERT OR IGNORE INTO factions(name,name_key) VALUES(?,?)", (label, name_key(label)))
                names = await rows(conn, "SELECT name FROM item_names UNION SELECT item_name AS name FROM gp_stock UNION SELECT item_name AS name FROM storage_items UNION SELECT item_name AS name FROM market_order_items")
                for r in names:
                    await conn.execute("INSERT OR IGNORE INTO catalogue(name,name_key,updated_at) VALUES(?,?,?)", (r['name'], name_key(r['name']), now()))
                # SQLite NOCASE does not fold Cyrillic. Merge spelling variants,
                # adding their quantities AND reservations without losing stock.
                for cat in await rows(conn, "SELECT * FROM catalogue"):
                    stocks = [s for s in await rows(conn, "SELECT * FROM gp_stock") if name_key(s['item_name']) == cat['name_key']]
                    if stocks:
                        for s in stocks:
                            await conn.execute("DELETE FROM gp_stock WHERE id=?", (s['id'],))
                        await conn.execute("INSERT INTO gp_stock(item_name,quantity,reserved,updated_at) VALUES(?,?,?,?)",
                                           (cat['name'], sum(s['quantity'] for s in stocks), sum(s['reserved'] for s in stocks), now()))
                    for table in ('storage_items', 'market_order_items'):
                        for item in await rows(conn, f"SELECT id,item_name FROM {table}"):
                            if name_key(item['item_name']) == cat['name_key']:
                                await conn.execute(f"UPDATE {table} SET item_name=? WHERE id=?", (cat['name'], item['id']))
                own = (await rows(conn, "SELECT id FROM factions WHERE name_key=?", (name_key('Полдень'),)))[0]['id']
                for r in await rows(conn, "SELECT * FROM diplomacy_records"):
                    await conn.execute("INSERT OR IGNORE INTO factions(name,name_key) VALUES(?,?)", (r['faction_name'], name_key(r['faction_name'])))
                    other = (await rows(conn, "SELECT id FROM factions WHERE name_key=?", (name_key(r['faction_name']),)))[0]['id']
                    if own != other:
                        await conn.execute("INSERT OR IGNORE INTO faction_relations VALUES(?,?,?,?,?)", (*sorted((own, other)), r['relation'], r['updated_by'], r['updated_at']))
                await conn.execute("INSERT INTO bot_settings(key,value,updated_at) VALUES('community_migration_v1','1',?)", (now(),))
                await self._reconcile_targets(conn, 0)
            await conn.commit()

    async def community_rows(self, sql, args=()):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            return await rows(conn, sql, args)

    async def advance_order(self, order_id, status, actor):
        transitions = {'pending': {'accepted','rejected'}, 'accepted': {'assembled','rejected'}, 'assembled': {'issued'}}
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute('BEGIN IMMEDIATE')
            found = await rows(conn, 'SELECT workflow_status FROM market_orders WHERE id=?', (order_id,))
            if not found or status not in transitions.get(found[0]['workflow_status'], set()):
                raise ValueError('Этот переход статуса уже недоступен.')
            previous = found[0]['workflow_status']
            is_trader = bool(await rows(conn, 'SELECT 1 FROM trader_orders WHERE order_id=?', (order_id,)))
            if not is_trader:
                # Orders created before the split keep their original reserves.
                items = await rows(conn, 'SELECT item_name,SUM(quantity) AS quantity FROM market_order_items WHERE order_id=? GROUP BY item_name COLLATE NOCASE', (order_id,))
                for item in items:
                    name, qty = item['item_name'],item['quantity']
                    if status == 'accepted':
                        cur = await conn.execute('UPDATE gp_stock SET reserved=reserved+?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND quantity-reserved>=?', (qty,actor,now(),name,qty))
                    elif status == 'issued':
                        cur = await conn.execute('UPDATE gp_stock SET quantity=quantity-?,reserved=reserved-?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND quantity>=? AND reserved>=?', (qty,qty,actor,now(),name,qty,qty))
                    elif status == 'rejected' and previous == 'accepted':
                        cur = await conn.execute('UPDATE gp_stock SET reserved=reserved-?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND reserved>=?', (qty,actor,now(),name,qty))
                    else:
                        continue
                    if cur.rowcount != 1:
                        raise ValueError(f'Недостаточно остатка или резерва для старого заказа: {name}.')
            await conn.execute('UPDATE market_orders SET workflow_status=? WHERE id=?', (status,order_id))
            await audit(conn,actor,'trader.status',f'#{order_id} {previous} → {status}')
            await conn.commit()

    async def catalogue_save(self, name):
        name = clean_name(name)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("INSERT INTO catalogue(name,name_key,updated_at) VALUES(?,?,?) ON CONFLICT(name_key) DO UPDATE SET archived=0,updated_at=excluded.updated_at", (name, name_key(name), now()))
            item = (await rows(conn, "SELECT * FROM catalogue WHERE name_key=?", (name_key(name),)))[0]
            await conn.commit()
            return item

    async def catalogue_list(self, page=0, search="", archived=False):
        return await self.community_rows("""SELECT c.*,COALESCE(s.quantity,0) AS quantity,COALESCE(s.reserved,0) AS reserved
            FROM catalogue c LEFT JOIN gp_stock s ON s.item_name=c.name COLLATE NOCASE
            WHERE c.archived=? AND instr(c.name_key,?)>0 ORDER BY c.name_key LIMIT 9 OFFSET ?""", (int(archived), name_key(search), max(0,page)*8))

    async def catalogue_get(self, item_id):
        result = await self.community_rows("""SELECT c.*,COALESCE(s.quantity,0) AS quantity,COALESCE(s.reserved,0) AS reserved
            FROM catalogue c LEFT JOIN gp_stock s ON s.item_name=c.name COLLATE NOCASE WHERE c.id=?""", (item_id,))
        if not result:
            raise ValueError("Позиция не найдена.")
        return result[0]

    async def catalogue_edit(self, item_id, actor, *, name=None, archived=None):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            cat = (await rows(conn, "SELECT * FROM catalogue WHERE id=?", (item_id,)))[0]
            if name is not None:
                name = clean_name(name)
                if await rows(conn, "SELECT id FROM catalogue WHERE name_key=? AND id!=?", (name_key(name), item_id)):
                    raise ValueError("Такое название уже есть в каталоге. Выберите существующую позицию.")
                for table in ('gp_stock', 'storage_items', 'market_order_items'):
                    await conn.execute(f"UPDATE {table} SET item_name=? WHERE item_name=? COLLATE NOCASE", (name, cat['name']))
                await conn.execute("DELETE FROM item_names WHERE name=? COLLATE NOCASE", (cat['name'],))
                await conn.execute("INSERT OR IGNORE INTO item_names(name,last_used_at) VALUES(?,?)", (name, now()))
                await conn.execute("UPDATE catalogue SET name=?,name_key=?,updated_at=? WHERE id=?", (name, name_key(name), now(), item_id))
            if archived is not None:
                if archived:
                    stock = await rows(conn, "SELECT quantity,reserved FROM gp_stock WHERE item_name=? COLLATE NOCASE", (cat['name'],))
                    active = await rows(conn, "SELECT id FROM warehouse_requests WHERE item_id=? AND status IN ('pending','approved')", (item_id,))
                    if active or any(s['quantity'] or s['reserved'] for s in stock):
                        raise ValueError("Сначала обнулите свободный остаток и завершите заявки. Резерв удалять нельзя.")
                await conn.execute("UPDATE catalogue SET archived=?,updated_at=? WHERE id=?", (int(archived), now(), item_id))
            await audit(conn, actor, 'catalogue.edit', f'#{item_id} name={name} archived={archived}')
            await conn.commit()

    async def warehouse_create(self, user, item_id, quantity, reason, request_key):
        if not 1 <= quantity <= 1000000 or not 1 <= len(reason) <= 500:
            raise ValueError("Количество: 1–1000000. Причина: 1–500 символов.")
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            await require_role(conn, user)
            cat = await rows(conn, "SELECT * FROM catalogue WHERE id=? AND archived=0", (item_id,))
            if not cat:
                raise ValueError("Эта позиция удалена из активного каталога.")
            stock = await rows(conn, "SELECT quantity-reserved AS available FROM gp_stock WHERE item_name=? COLLATE NOCASE", (cat[0]['name'],))
            if not stock or stock[0]['available'] < quantity:
                raise ValueError("Недостаточно свободного имущества на складе.")
            await conn.execute("INSERT OR IGNORE INTO warehouse_requests(requester_id,item_id,quantity,reason,created_at,updated_at,request_key) VALUES(?,?,?,?,?,?,?)", (user,item_id,quantity,reason,now(),now(),request_key))
            result = (await rows(conn, "SELECT id FROM warehouse_requests WHERE request_key=?", (request_key,)))[0]['id']
            await audit(conn, user, 'warehouse.request', result)
            await conn.commit()
            return result

    async def warehouse_transition(self, request_id, action, actor):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            rr = await rows(conn, "SELECT r.*,c.name FROM warehouse_requests r JOIN catalogue c ON c.id=r.item_id WHERE r.id=?", (request_id,))
            if not rr:
                raise ValueError("Заявка не найдена.")
            r = rr[0]
            if action in ('approve','reject'):
                await require_role(conn, actor, {'leader','deputy_leader'})
                expected, status = 'pending', 'approved' if action=='approve' else 'rejected'
            elif action=='issue':
                await require_role(conn, actor, {'storekeeper'})
                expected, status = 'approved', 'issued'
            elif action=='cancel':
                p = await require_role(conn, actor)
                if actor != r['requester_id'] and p['position_code'] not in {'leader','deputy_leader'}:
                    raise ValueError("Отменить может заявитель или руководство.")
                expected, status = r['status'], 'cancelled'
                if expected not in ('pending','approved'):
                    raise ValueError("Заявка уже завершена.")
            else:
                raise ValueError("Неизвестное действие.")
            if r['status'] != expected:
                raise ValueError("Заявка уже обработана. Откройте актуальную очередь.")
            if action=='approve':
                cur = await conn.execute("UPDATE gp_stock SET reserved=reserved+?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND quantity-reserved>=?", (r['quantity'],actor,now(),r['name'],r['quantity']))
                if cur.rowcount != 1:
                    raise ValueError("Остатка недостаточно: другое одобрение уже заняло имущество.")
            if action=='issue':
                cur = await conn.execute("UPDATE gp_stock SET quantity=quantity-?,reserved=reserved-?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND reserved>=? AND quantity>=?", (r['quantity'],r['quantity'],actor,now(),r['name'],r['quantity'],r['quantity']))
                if cur.rowcount != 1:
                    raise ValueError("Резерв повреждён. Проверьте склад.")
            if action=='cancel' and expected=='approved':
                cur = await conn.execute("UPDATE gp_stock SET reserved=reserved-?,updated_by=?,updated_at=? WHERE item_name=? COLLATE NOCASE AND reserved>=?", (r['quantity'],actor,now(),r['name'],r['quantity']))
                if cur.rowcount != 1:
                    raise ValueError("Резерв повреждён. Проверьте склад.")
            await conn.execute("UPDATE warehouse_requests SET status=?,reviewed_by=CASE WHEN ? IN ('approve','reject') THEN ? ELSE reviewed_by END,issued_by=CASE WHEN ?='issue' THEN ? ELSE issued_by END,updated_at=? WHERE id=?", (status,action,actor,action,actor,now(),request_id))
            await audit(conn, actor, 'warehouse.'+action, request_id)
            await conn.commit()

    async def warehouse_list(self, page=0, user=None, history=False):
        return await self.community_rows("""SELECT r.*,c.name,p.game_nickname FROM warehouse_requests r
            JOIN catalogue c ON c.id=r.item_id JOIN players p ON p.telegram_id=r.requester_id
            WHERE (? IS NULL OR r.requester_id=?) AND
            (?=-1 OR (?=0 AND r.status IN ('pending','approved')) OR (?=1 AND r.status NOT IN ('pending','approved')))
            ORDER BY r.id DESC LIMIT 9 OFFSET ?""", (user,user,int(history),int(history),int(history),max(0,page)*8))

    async def ad_create(self, user, item_id, photo, price, description, request_key):
        if not photo or not 1 <= price <= 1000000000000 or not 1 <= len(description) <= 600:
            raise ValueError("Нужны фотография, цена от 1 до 1000000000000 ₽ и описание до 600 символов.")
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            await require_role(conn, user)
            if not await rows(conn, "SELECT id FROM catalogue WHERE id=? AND archived=0", (item_id,)):
                raise ValueError("Позиция удалена. Выберите её заново.")
            await conn.execute("INSERT OR IGNORE INTO sale_ads(seller_id,item_id,photo_id,price,description,created_at,updated_at,request_key) VALUES(?,?,?,?,?,?,?,?)", (user,item_id,photo,price,description,now(),now(),request_key))
            result = (await rows(conn, "SELECT id FROM sale_ads WHERE request_key=?", (request_key,)))[0]['id']
            await audit(conn, user, 'sale.create', result)
            await conn.commit()
            return result

    async def ad_close(self, ad_id, user, status, admin=False):
        if status not in ('sold','removed'):
            raise ValueError("Неверный статус.")
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            result = await rows(conn, "SELECT * FROM sale_ads WHERE id=?", (ad_id,))
            if not result:
                raise ValueError("Объявление не найдено.")
            r = result[0]
            if not admin:
                p = await require_role(conn, user)
                if r['seller_id'] != user and p['position_code'] not in ('leader','deputy_leader','trader'):
                    raise ValueError("Изменить объявление может продавец или модератор рынка.")
            if r['status'] != 'active':
                raise ValueError("Объявление уже закрыто.")
            await conn.execute("UPDATE sale_ads SET status=?,updated_at=? WHERE id=?", (status,now(),ad_id))
            await audit(conn, user, 'sale.'+status, ad_id)
            await conn.commit()

    async def ad_list(self, page=0, user=None):
        return await self.community_rows("""SELECT a.*,c.name,p.game_nickname FROM sale_ads a JOIN catalogue c ON c.id=a.item_id
            JOIN players p ON p.telegram_id=a.seller_id WHERE (? IS NULL AND a.status='active') OR a.seller_id=?
            ORDER BY a.id DESC LIMIT 9 OFFSET ?""", (user,user,max(0,page)*8))

    async def faction_add(self, name, actor):
        name = clean_name(name)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            if len(await rows(conn, "SELECT id FROM factions")) >= 30:
                raise ValueError("Достигнут лимит 30 группировок.")
            await conn.execute("INSERT OR IGNORE INTO factions(name,name_key) VALUES(?,?)", (name,name_key(name)))
            await audit(conn, actor, 'faction.add', name)
            await conn.commit()

    async def relation_set(self, a, b, relation, actor):
        if a==b or relation not in ('ally','neutral','war','unknown'):
            raise ValueError("Выберите две разные группировки и отношение.")
        a,b = sorted((a,b))
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            if len(await rows(conn, "SELECT id FROM factions WHERE id IN (?,?)", (a,b))) != 2:
                raise ValueError("Группировка не найдена.")
            previous = await rows(conn, "SELECT relation FROM faction_relations WHERE a=? AND b=?", (a,b))
            if (previous[0]['relation'] if previous else 'unknown') == relation:
                return
            if relation=='unknown':
                await conn.execute("DELETE FROM faction_relations WHERE a=? AND b=?", (a,b))
            else:
                await conn.execute("INSERT INTO faction_relations VALUES(?,?,?,?,?) ON CONFLICT(a,b) DO UPDATE SET relation=excluded.relation,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (a,b,relation,actor,now()))
            await audit(conn, actor, 'diplomacy.pair', f'{a}/{b}={relation}')
            await self._reconcile_targets(conn, actor)
            await conn.commit()

    async def _target_basis(self, conn):
        factions = await rows(conn, "SELECT * FROM factions ORDER BY id")
        own = next(f['id'] for f in factions if f['name_key']==name_key('Полдень'))
        relations = await rows(conn, "SELECT * FROM faction_relations")
        mapping = {(r['a'],r['b']): r['relation'] for r in relations}
        versions = {(r['a'],r['b']): r['updated_at'] for r in relations}
        def relation(a,b):
            return mapping.get(tuple(sorted((a,b))))
        allies = {f['id'] for f in factions if relation(own,f['id'])=='ally'}
        direct = {f['id'] for f in factions if relation(own,f['id'])=='war'}
        candidates = {}
        for f in factions:
            if f['id']==own or f['id'] in allies or f['id'] in direct:
                continue
            enemies = sorted(a for a in allies if relation(a,f['id'])=='war')
            if enemies:
                candidates[f['id']] = json.dumps([(a,versions[tuple(sorted((own,a)))],versions[tuple(sorted((a,f['id'])))]) for a in enemies])
        return factions, direct, candidates

    async def _reconcile_targets(self, conn, actor):
        factions, direct, candidates = await self._target_basis(conn)
        # Decisions expire when their alliance/war basis changes, including after
        # peace followed by a new war. No transitive propagation of hostility.
        decisions = await rows(conn, "SELECT * FROM ally_target_decisions")
        for d in decisions:
            if candidates.get(d['faction_id']) != d['basis']:
                await conn.execute("DELETE FROM ally_target_decisions WHERE faction_id=?", (d['faction_id'],))
        approved = {d['faction_id'] for d in decisions if d['approved'] and candidates.get(d['faction_id'])==d['basis']}
        desired = direct | approved
        names = {f['id']: f['name'] for f in factions}
        links = {r['faction_id']: r['target_id'] for r in await rows(conn, "SELECT * FROM faction_targets")}
        for fid in desired:
            reason = 'Война с Полднем' if fid in direct else 'Враг союзника; одобрено Дипломатом'
            if fid not in links:
                cur = await conn.execute("INSERT INTO targets(target_name,reason,status,created_by,created_at,updated_at) VALUES(?,?,'active',?,?,?)", (names[fid],reason,actor,now(),now()))
                await conn.execute("INSERT INTO faction_targets VALUES(?,?)", (fid,cur.lastrowid))
            else:
                await conn.execute("UPDATE targets SET target_name=?,reason=?,status='active',assigned_to=NULL,updated_at=? WHERE id=?", (names[fid],reason,now(),links[fid]))
        for fid, tid in links.items():
            if fid not in desired:
                await conn.execute("UPDATE targets SET status='cancelled',assigned_to=NULL,updated_at=? WHERE id=?", (now(),tid))

    async def ally_candidates(self):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            factions, _, candidates = await self._target_basis(conn)
            decisions = {d['faction_id']: d for d in await rows(conn, "SELECT * FROM ally_target_decisions")}
            names = {f['id']: f['name'] for f in factions}
            return [dict(id=fid,name=names[fid],basis=basis,allies=', '.join(names[i[0]] for i in json.loads(basis)),decision=decisions.get(fid)) for fid,basis in candidates.items()]

    async def ally_decide(self, faction_id, basis, approved, actor):
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            await require_role(conn, actor, {'diplomat'})
            _, _, candidates = await self._target_basis(conn)
            if candidates.get(faction_id) != basis:
                raise ValueError("Отношения изменились. Откройте актуальный список кандидатов.")
            await conn.execute("INSERT INTO ally_target_decisions VALUES(?,?,?,?,?) ON CONFLICT(faction_id) DO UPDATE SET basis=excluded.basis,approved=excluded.approved,actor_id=excluded.actor_id,updated_at=excluded.updated_at", (faction_id,basis,int(approved),actor,now()))
            await audit(conn, actor, 'diplomacy.ally_decision', f'{faction_id} approved={approved} basis={basis}')
            await self._reconcile_targets(conn, actor)
            await conn.commit()
