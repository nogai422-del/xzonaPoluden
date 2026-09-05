CREATE TABLE players (
 telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL,
 game_nickname TEXT NOT NULL COLLATE NOCASE UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE item_names (
 name TEXT PRIMARY KEY COLLATE NOCASE, use_count INTEGER NOT NULL DEFAULT 1, last_used_at TEXT NOT NULL
);
CREATE TABLE storage_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL, item_name TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0), comment TEXT,
 status TEXT NOT NULL DEFAULT 'stored' CHECK(status IN ('stored','issued')),
 accepted_by INTEGER NOT NULL, accepted_at TEXT NOT NULL, issued_by INTEGER, issued_at TEXT,
 FOREIGN KEY(player_id) REFERENCES players(telegram_id) ON DELETE RESTRICT
);
CREATE TABLE gp_stock (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
 quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0), reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
 updated_by INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE diplomacy_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, faction_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
 relation TEXT NOT NULL CHECK(relation IN ('ally','neutral','war')), note TEXT,
 updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO players VALUES(4,NULL,'User','Игрок','2026-09-01','2026-09-01');
INSERT INTO item_names(name,last_used_at) VALUES('Аптечка','2026-09-01');
INSERT INTO storage_items(player_id,item_name,quantity,accepted_by,accepted_at) VALUES(4,'Аптечка',2,3,'2026-09-01');
INSERT INTO gp_stock(item_name,quantity,reserved,updated_by,updated_at) VALUES('Аптечка',8,2,3,'2026-09-01');
INSERT INTO gp_stock(item_name,quantity,reserved,updated_by,updated_at) VALUES('АПТЕЧКА',4,0,3,'2026-09-01');
INSERT INTO diplomacy_records(faction_name,relation,updated_by,updated_at) VALUES('Долг','war',1,'2026-09-01');
