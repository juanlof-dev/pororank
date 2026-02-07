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
