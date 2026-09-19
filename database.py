import os
import sqlite3
import json

DB_FILE = os.path.join("/data", "accounts.db")

def get_conn():
    return sqlite3.connect(DB_FILE)

def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            user_id TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS unlinked_tracking (
            user_id TEXT PRIMARY KEY,
            unlinked_since TEXT NOT NULL,
            reminder_24h_sent INTEGER NOT NULL DEFAULT 0,
            reminder_72h_sent INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS active_groups (
            thread_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            creator_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            warned INTEGER NOT NULL DEFAULT 0
        )
        """)

        # Migración: si active_groups ya existía de un despliegue anterior a
        # que añadiéramos 'warned', CREATE TABLE IF NOT EXISTS no la habría
        # creado (la tabla ya existía). Lo comprobamos y la añadimos a mano.
        cols = [row[1] for row in conn.execute("PRAGMA table_info(active_groups)").fetchall()]
        if "warned" not in cols:
            conn.execute("ALTER TABLE active_groups ADD COLUMN warned INTEGER NOT NULL DEFAULT 0")

def load_data():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, data FROM accounts")
        rows = cur.fetchall()

    return {uid: json.loads(data) for uid, data in rows}

def save_data(data):
    with get_conn() as conn:
        cur = conn.cursor()
        for uid, accs in data.items():
            cur.execute("""
            INSERT INTO accounts (user_id, data)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET data=excluded.data

            """, (uid, json.dumps(accs)))

def has_linked_before(user_id):
    """True si esta persona tiene fila en accounts (vinculó alguna vez,
    aunque ahora mismo se haya quedado sin ninguna cuenta activa)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM accounts WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def get_unlinked_tracking(user_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT unlinked_since, reminder_24h_sent, reminder_72h_sent "
            "FROM unlinked_tracking WHERE user_id=?", (user_id,)
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "unlinked_since": row[0],
        "reminder_24h_sent": bool(row[1]),
        "reminder_72h_sent": bool(row[2]),
    }

def get_all_unlinked_tracking():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, unlinked_since, reminder_24h_sent, reminder_72h_sent FROM unlinked_tracking")
        rows = cur.fetchall()
    return {
        uid: {
            "unlinked_since": since,
            "reminder_24h_sent": bool(r24),
            "reminder_72h_sent": bool(r72),
        }
        for uid, since, r24, r72 in rows
    }

def start_unlinked_tracking(user_id, unlinked_since_iso):
    """Crea o REINICIA el seguimiento (recordatorios a 0) de alguien que
    empieza a estar sin vincular. Usar solo para quien nunca ha vinculado
    nada — para quien ya vinculó antes, no se llama a esta función."""
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO unlinked_tracking (user_id, unlinked_since, reminder_24h_sent, reminder_72h_sent)
        VALUES (?, ?, 0, 0)
        ON CONFLICT(user_id)
        DO UPDATE SET unlinked_since=excluded.unlinked_since, reminder_24h_sent=0, reminder_72h_sent=0
        """, (user_id, unlinked_since_iso))

def stop_unlinked_tracking(user_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM unlinked_tracking WHERE user_id=?", (user_id,))

def mark_reminder_sent(user_id, which):
    """which: '24h' o '72h'"""
    column = "reminder_24h_sent" if which == "24h" else "reminder_72h_sent"
    with get_conn() as conn:
        conn.execute(f"UPDATE unlinked_tracking SET {column}=1 WHERE user_id=?", (user_id,))

def create_group(thread_id, message_id, channel_id, creator_id, created_at_iso):
    """Registra un grupo de 'Buscar partida' recién creado, para poder
    reconstruir sus botones tras un reinicio y para el borrado automático."""
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO active_groups (thread_id, message_id, channel_id, creator_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            message_id=excluded.message_id, channel_id=excluded.channel_id,
            creator_id=excluded.creator_id, created_at=excluded.created_at
        """, (str(thread_id), str(message_id), str(channel_id), str(creator_id), created_at_iso))

def get_group(thread_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT thread_id, message_id, channel_id, creator_id, created_at, warned "
            "FROM active_groups WHERE thread_id=?", (str(thread_id),)
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"thread_id": row[0], "message_id": row[1], "channel_id": row[2],
            "creator_id": row[3], "created_at": row[4], "warned": bool(row[5])}

def get_all_groups():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT thread_id, message_id, channel_id, creator_id, created_at, warned FROM active_groups")
        rows = cur.fetchall()
    return [
        {"thread_id": tid, "message_id": mid, "channel_id": cid, "creator_id": crid,
         "created_at": ca, "warned": bool(w)}
        for tid, mid, cid, crid, ca, w in rows
    ]

def delete_group(thread_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM active_groups WHERE thread_id=?", (str(thread_id),))

def mark_group_warned(thread_id):
    with get_conn() as conn:
        conn.execute("UPDATE active_groups SET warned=1 WHERE thread_id=?", (str(thread_id),))
