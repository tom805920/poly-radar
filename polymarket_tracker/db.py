from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


DB_PATH = Path("data/polymarket_tracker.sqlite")
logger = logging.getLogger(__name__)


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


def make_json_safe(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        number = float(value)
        return number if math.isfinite(number) else None

    try:
        import numpy as np

        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            number = float(value)
            return number if math.isfinite(number) else None
        if isinstance(value, np.ndarray):
            return [make_json_safe(item) for item in value.tolist()]
        if isinstance(value, np.bool_):
            return bool(value)
    except ImportError:
        pass

    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
    except ImportError:
        pass

    try:
        if math.isnan(value) or math.isinf(value):
            return None
    except (TypeError, ValueError):
        pass

    return str(value)


def set_cache(con: sqlite3.Connection, key: str, payload: Any) -> None:
    import time

    try:
        safe_payload = make_json_safe(payload)
        con.execute(
            "REPLACE INTO api_cache(cache_key, payload, fetched_at) VALUES (?, ?, ?)",
            (str(key), json.dumps(safe_payload, allow_nan=False), int(time.time())),
        )
        con.commit()
    except (TypeError, ValueError, sqlite3.Error):
        logger.warning("Could not save API cache payload", exc_info=True)


def save_wallet_score(con: sqlite3.Connection, wallet: str, payload: dict[str, Any]) -> None:
    import time

    try:
        safe_wallet = str(wallet).lower()
        safe_payload = make_json_safe(payload)
        payload_json = json.dumps(safe_payload, allow_nan=False)
        con.execute(
            "REPLACE INTO wallet_scores(wallet, payload, calculated_at) VALUES (?, ?, ?)",
            (safe_wallet, payload_json, int(time.time())),
        )
        con.commit()
    except (TypeError, ValueError, sqlite3.Error):
        logger.warning("Could not save wallet score for %s", wallet, exc_info=True)
