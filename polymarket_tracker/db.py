from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


DB_PATH = Path("data/polymarket_tracker.sqlite")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS api_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_scores (
            wallet TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            calculated_at INTEGER NOT NULL
        )
        """
    )
    con.commit()
    return con


def get_cache(con: sqlite3.Connection, key: str, max_age_seconds: int) -> Any | None:
    row = con.execute("SELECT payload, fetched_at FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    payload, fetched_at = row
    import time

    if int(time.time()) - int(fetched_at) > max_age_seconds:
        return None
    return json.loads(payload)


def set_cache(con: sqlite3.Connection, key: str, payload: Any) -> None:
    import time

    con.execute(
        "REPLACE INTO api_cache(cache_key, payload, fetched_at) VALUES (?, ?, ?)",
        (key, json.dumps(payload), int(time.time())),
    )
    con.commit()


def save_wallet_score(con: sqlite3.Connection, wallet: str, payload: dict[str, Any]) -> None:
    import time

    con.execute(
        "REPLACE INTO wallet_scores(wallet, payload, calculated_at) VALUES (?, ?, ?)",
        (wallet.lower(), json.dumps(payload), int(time.time())),
    )
    con.commit()
