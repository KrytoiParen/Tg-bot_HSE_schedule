import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from config import DB_PATH


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def create_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                program TEXT NOT NULL,
                group_id TEXT NOT NULL,
                en TEXT NOT NULL DEFAULT '[]',
                morning TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                snapshot TEXT,
                checked TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS cache (
                name TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                checked TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                part INTEGER NOT NULL,
                text TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, tag, part)
            );
        """)


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_user(row):
    if row is None:
        return None
    user = dict(row)
    user["en"] = json.loads(user["en"])
    user["snapshot"] = json.loads(user["snapshot"]) if user["snapshot"] is not None else None
    return user


def get_user(user_id):
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return read_user(row)


def get_users():
    with connect() as db:
        rows = db.execute("SELECT * FROM users WHERE active=1").fetchall()
    return [read_user(row) for row in rows]


def save_user(user_id, program, group_id, en, morning, snapshot):
    with connect() as db:
        db.execute("DELETE FROM mail WHERE user_id=? AND sent=0", (user_id,))
        db.execute(
            """INSERT INTO users(id, program, group_id, en, morning, snapshot, checked)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            program=excluded.program, group_id=excluded.group_id, en=excluded.en,
            morning=excluded.morning, snapshot=excluded.snapshot, checked=excluded.checked,
            active=1, error=NULL""",
            (user_id, program, group_id, dump(en), morning, dump(snapshot), now()),
        )


def stop_user(user_id):
    with connect() as db:
        db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        db.execute("DELETE FROM mail WHERE user_id=? AND sent=0", (user_id,))


def set_time(user_id, morning):
    with connect() as db:
        db.execute("UPDATE users SET morning=? WHERE id=?", (morning, user_id))


def get_cache(name):
    with connect() as db:
        row = db.execute("SELECT data FROM cache WHERE name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else None


def save_cache(name, data):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (name, dump(data), now()))


def add_mail(db, user_id, tag, texts):
    db.executemany(
        "INSERT OR IGNORE INTO mail(user_id,tag,part,text) VALUES(?,?,?,?)",
        [(user_id, tag, i, dump({"rich_message": text}) if isinstance(text, dict) else text)
         for i, text in enumerate(texts)],
    )


def save_plan(user_id, snapshot, texts=()):
    # Записываем новую версию и уведомление одной транзакцией.
    with connect() as db:
        if texts:
            add_mail(db, user_id, "change:" + now(), texts)
        db.execute("DELETE FROM mail WHERE user_id=? AND sent=0 AND tag LIKE 'error:%'", (user_id,))
        db.execute("UPDATE users SET snapshot=?, checked=?, error=NULL WHERE id=?", (dump(snapshot), now(), user_id))


def save_error(user_id, message, texts=()):
    with connect() as db:
        old = db.execute("SELECT error FROM users WHERE id=?", (user_id,)).fetchone()
        if old and old[0] != message and texts:
            add_mail(db, user_id, "error:" + now(), texts)
        db.execute("UPDATE users SET error=? WHERE id=?", (message, user_id))


def add_morning(user_id, day, texts):
    with connect() as db:
        add_mail(db, user_id, "morning:" + day, texts)


def get_mail():
    with connect() as db:
        rows = db.execute("""SELECT m.* FROM mail m JOIN users u ON u.id=m.user_id
            WHERE m.sent=0 AND u.active=1 ORDER BY m.id LIMIT 100""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Старые текстовые сообщения остаются совместимыми с новой очередью.
        if item["text"].startswith('{"rich_message":'):
            try:
                payload = json.loads(item["text"])
                if isinstance(payload.get("rich_message"), dict):
                    item["text"] = payload["rich_message"]
            except (ValueError, AttributeError):
                pass
        result.append(item)
    return result


def mark_sent(mail_id):
    with connect() as db:
        db.execute("UPDATE mail SET sent=1 WHERE id=?", (mail_id,))
