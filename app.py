from __future__ import annotations

import time
import logging
from dataclasses import dataclass

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from polymarket_tracker.api import (
    MAX_OFFSET,
    PRICE_HISTORY_DAY_SECONDS,
    PRICE_HISTORY_FALLBACK_SECONDS,
    PolymarketAPIError,
    PolymarketClient,
)
from polymarket_tracker.db import connect, get_cache, save_wallet_score, set_cache
from polymarket_tracker.metrics import (
    CATEGORY_KEYWORDS,
    FilterSettings,
    apply_filters,
    bot_likeness_warning,
    filter_items_by_period_and_category,
    normalize_wallets,
    normalize_to_100,
    repetitive_size_ratio,
    score_wallet,
)
from polymarket_tracker.supabase_store import (
    DEFAULT_USER_SETTINGS,
    SUPABASE_CONNECTION_ERROR,
    SupabaseError,
    SupabaseStore,
)


st.set_page_config(page_title="Poly Radar", layout="wide")
logger = logging.getLogger(__name__)

MAX_RECENT_TRADES_FAST = 1000
MAX_WALLETS_FAST = 50
MAX_API_CALLS_PER_RUN = 100
MAX_RUN_SECONDS = 60


def api_client() -> PolymarketClient:
    return PolymarketClient()


@st.cache_resource
def db_connection():
    return connect()


def read_supabase_secrets() -> tuple[str, str] | None:
    try:
        url = str(st.secrets["SUPABASE_URL"]).strip().rstrip("/")
        anon_key = str(st.secrets["SUPABASE_ANON_KEY"]).strip()
    except Exception:
        return None
    if not url or not anon_key:
        return None
    return url, anon_key


@st.cache_resource
def supabase_store(url: str, anon_key: str) -> SupabaseStore:
    return SupabaseStore(url, anon_key)


def get_supabase_store() -> SupabaseStore | None:
    config = read_supabase_secrets()
    if not config:
        return None
    return supabase_store(*config)


def show_supabase_error(action: str, exc: SupabaseError) -> None:
    if exc.status_code is None and str(exc) == SUPABASE_CONNECTION_ERROR:
        st.error(SUPABASE_CONNECTION_ERROR)
    else:
        st.error(f"{action} failed: {exc}")


def reset_user_runtime_state() -> None:
    for key in [
        "watchlist_items",
        "watchlist",
        "seen_trade_ids",
        "seen_trade_timestamps",
        "watchlist_alert_wallets_initialized",
        "new_trade_alerts",
        "user_settings",
        "rows",
        "discovered_wallets",
        "recent_trades",
        "selected_wallet",
    ]:
        st.session_state.pop(key, None)


def save_auth_session(session) -> None:
    st.session_state["supabase_session"] = session.to_dict()


def current_auth_session() -> dict | None:
    session = st.session_state.get("supabase_session")
    if not session or not session.get("access_token") or not session.get("user_id"):
        return None
    if int(session.get("expires_at") or 0) <= int(time.time()) + 60:
        refresh_token = session.get("refresh_token")
        store = get_supabase_store()
        if not refresh_token or not store:
            st.session_state.pop("supabase_session", None)
            return None
        try:
            refreshed = store.refresh_session(str(refresh_token))
        except SupabaseError:
            logger.warning("Could not refresh Supabase session", exc_info=True)
            st.session_state.pop("supabase_session", None)
            return None
        save_auth_session(refreshed)
        session = st.session_state["supabase_session"]
    return session


def render_auth_page() -> None:
    st.title("Poly Radar")
    st.caption("Sign in to keep your watchlist, alerts, and settings private to your account.")
    store = get_supabase_store()
    if not store:
        st.error("Supabase is not configured. Add SUPABASE_URL and SUPABASE_ANON_KEY to Streamlit secrets.")
        st.stop()

    st.caption(f"Supabase URL: `{store.url}`")
    if st.button("Test Supabase connection"):
        try:
            store.test_connection()
        except SupabaseError as exc:
            show_supabase_error("Supabase connection test", exc)
        else:
            st.success("Supabase connection looks good.")

    login_tab, signup_tab = st.tabs(["Log in", "Sign up"])
    with login_tab:
        with st.form("login-form"):
            email = st.text_input("Email", key="login-email")
            password = st.text_input("Password", type="password", key="login-password")
            submitted = st.form_submit_button("Log in", type="primary")
        if submitted:
            if not email.strip() or not password:
                st.error("Enter both email and password.")
                st.stop()
            try:
                session = store.sign_in(email.strip(), password)
            except SupabaseError as exc:
                show_supabase_error("Login", exc)
            else:
                reset_user_runtime_state()
                save_auth_session(session)
                st.rerun()

    with signup_tab:
        with st.form("signup-form"):
            signup_email = st.text_input("Email", key="signup-email")
            signup_password = st.text_input("Password", type="password", key="signup-password")
            signup_submitted = st.form_submit_button("Create account")
        if signup_submitted:
            if not signup_email.strip() or not signup_password:
                st.error("Enter both email and password.")
                st.stop()
            try:
                session = store.sign_up(signup_email.strip(), signup_password)
            except SupabaseError as exc:
                show_supabase_error("Signup", exc)
            else:
                if session:
                    reset_user_runtime_state()
                    save_auth_session(session)
                    st.rerun()
                else:
                    st.success("Account created. Check your email to confirm it, then log in.")


def sign_out() -> None:
    store = get_supabase_store()
    session = current_auth_session()
    if store and session:
        try:
            store.logout(str(session["access_token"]))
        except SupabaseError:
            logger.warning("Supabase logout request failed", exc_info=True)
    st.session_state.pop("supabase_session", None)
    st.session_state.pop("loaded_user_id", None)
    reset_user_runtime_state()
    st.rerun()


def sync_user_data_from_supabase(force: bool = False) -> None:
    session = current_auth_session()
    store = get_supabase_store()
    if not session or not store:
        return
    user_id = str(session["user_id"])
    if not force and st.session_state.get("loaded_user_id") == user_id:
        ensure_watchlist_state()
        return
    try:
        st.session_state["watchlist_items"] = store.fetch_watchlist(str(session["access_token"]), user_id)
        st.session_state["user_settings"] = store.fetch_user_settings(str(session["access_token"]), user_id)
        st.session_state["loaded_user_id"] = user_id
    except SupabaseError as exc:
        st.error(f"Could not load your Supabase data: {exc}")
        st.stop()
    ensure_watchlist_state()


def ensure_watchlist_state() -> None:
    if "watchlist_items" not in st.session_state:
        st.session_state["watchlist_items"] = []
    st.session_state["watchlist"] = [
        str(item.get("wallet", "")).lower()
        for item in st.session_state["watchlist_items"]
        if item.get("wallet")
    ]
    st.session_state.setdefault("seen_trade_ids", [])
    st.session_state.setdefault("seen_trade_timestamps", {})
    st.session_state.setdefault("watchlist_alert_wallets_initialized", [])
    st.session_state.setdefault("new_trade_alerts", [])


def add_wallet_to_watchlist(wallet: str, row: dict | None = None) -> None:
    wallet = wallet.lower()
    ensure_watchlist_state()
    session = current_auth_session()
    store = get_supabase_store()
    if not session or not store:
        st.error("Log in before adding wallets to your watchlist.")
        return
    try:
        saved_items = store.upsert_watchlist(
            str(session["access_token"]),
            str(session["user_id"]),
            wallet,
        )
    except SupabaseError as exc:
        st.error(f"Could not add wallet to watchlist: {exc}")
        return
    existing = {item["wallet"].lower(): item for item in st.session_state["watchlist_items"]}
    item = saved_items[0] if saved_items else existing.get(wallet, {"wallet": wallet})
    if row:
        item.update(
            {
                "wallet": wallet,
                "whale_score": row.get("whale_score"),
                "net_profit": row.get("net_profit"),
                "roi_pct": row.get("roi_pct"),
            }
        )
    existing[wallet] = item
    st.session_state["watchlist_items"] = list(existing.values())
    ensure_watchlist_state()


def remove_wallet_from_watchlist(wallet: str) -> None:
    wallet = wallet.lower()
    ensure_watchlist_state()
    session = current_auth_session()
    store = get_supabase_store()
    if not session or not store:
        st.error("Log in before changing your watchlist.")
        return
    try:
        store.delete_watchlist_wallet(str(session["access_token"]), str(session["user_id"]), wallet)
    except SupabaseError as exc:
        st.error(f"Could not remove wallet from watchlist: {exc}")
        return
    st.session_state["watchlist_items"] = [
        item for item in st.session_state["watchlist_items"] if str(item.get("wallet", "")).lower() != wallet
    ]
    st.session_state["watchlist_alert_wallets_initialized"] = [
        item
        for item in st.session_state.get("watchlist_alert_wallets_initialized", [])
        if str(item).lower() != wallet
    ]
    ensure_watchlist_state()


def user_settings() -> dict:
    return {**DEFAULT_USER_SETTINGS, **st.session_state.get("user_settings", {})}


def update_user_settings(**updates) -> None:
    current = user_settings()
    changed = any(current.get(key) != value for key, value in updates.items())
    if not changed:
        return
    session = current_auth_session()
    store = get_supabase_store()
    if not session or not store:
        return
    updated = {**current, **updates}
    try:
        st.session_state["user_settings"] = store.upsert_user_settings(
            str(session["access_token"]),
            str(session["user_id"]),
            updated,
        )
    except SupabaseError as exc:
        st.warning(f"Could not save settings: {exc}")


@dataclass
class RunBudget:
    started_at: float
    api_calls: int = 0
    max_api_calls: int = MAX_API_CALLS_PER_RUN
    max_seconds: int = MAX_RUN_SECONDS

    def can_continue(self) -> bool:
        return self.api_calls < self.max_api_calls and (time.time() - self.started_at) < self.max_seconds

    def mark_call(self) -> None:
        self.api_calls += 1


def cached_fetch(key: str, fetcher, ttl_seconds: int = 6 * 3600, budget: RunBudget | None = None):
    con = db_connection()
    cached = get_cache(con, key, ttl_seconds)
    if cached is not None:
        return cached
    if budget:
        if not budget.can_continue():
            raise TimeoutError("Run limit reached before fetching more API data.")
        budget.mark_call()
    payload = fetcher()
    set_cache(con, key, payload)
    return payload


@st.cache_data(ttl=30 * 60, show_spinner=False)
def cached_recent_trade_page(limit: int, offset: int) -> list[dict]:
    client = api_client()
    try:
        return client.fetch_trades_page(limit=limit, offset=offset)
    except PolymarketAPIError as exc:
        if exc.status_code == 400:
            return []
        raise


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_closed_positions(wallet: str) -> list[dict]:
    return api_client().fetch_closed_positions(wallet, max_rows=500)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_wallet_trades(wallet: str) -> list[dict]:
    return api_client().fetch_trades(wallet, max_rows=1000)


@st.cache_data(ttl=30, show_spinner=False)
def cached_wallet_recent_trades(wallet: str, limit: int = 250) -> list[dict]:
    return api_client().fetch_trades_page(limit=limit, offset=0, wallet=wallet)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_price_history(asset: str, start_ts: int, end_ts: int) -> list[dict]:
    return api_client().fetch_price_history(asset, start_ts, end_ts)


def fetch_price_history_chunked(asset: str, start_ts: int, end_ts: int) -> tuple[list[dict], bool]:
    points_by_time: dict[int, dict] = {}
    had_failure = False

    def fetch_window(window_start: int, window_end: int, chunk_size: int) -> list[dict]:
        try:
            return cached_price_history(asset, window_start, window_end)
        except PolymarketAPIError as exc:
            if exc.status_code == 400:
                return []
            raise

    cursor = start_ts
    while cursor < end_ts:
        window_end = min(cursor + PRICE_HISTORY_DAY_SECONDS, end_ts)
        try:
            chunk = fetch_window(cursor, window_end, PRICE_HISTORY_DAY_SECONDS)
        except PolymarketAPIError:
            chunk = []
            had_failure = True
        if not chunk and window_end > cursor + PRICE_HISTORY_FALLBACK_SECONDS:
            fallback_cursor = cursor
            while fallback_cursor < window_end:
                fallback_end = min(fallback_cursor + PRICE_HISTORY_FALLBACK_SECONDS, window_end)
                try:
                    fallback_chunk = fetch_window(
                        fallback_cursor,
                        fallback_end,
                        PRICE_HISTORY_FALLBACK_SECONDS,
                    )
                except PolymarketAPIError:
                    fallback_chunk = []
                    had_failure = True
                if not fallback_chunk:
                    had_failure = True
                for point in fallback_chunk:
                    timestamp = int(point.get("t") or 0)
                    if timestamp:
                        points_by_time[timestamp] = point
                fallback_cursor = fallback_end
        else:
            for point in chunk:
                timestamp = int(point.get("t") or 0)
                if timestamp:
                    points_by_time[timestamp] = point
        cursor = window_end

    merged = [points_by_time[timestamp] for timestamp in sorted(points_by_time)]
    return merged, had_failure or not merged


def discover_wallets_from_recent_trades(
    time_period_days: int,
    market_category: str,
    max_recent_trades: int,
    max_wallets: int,
    progress=None,
    status=None,
    started_at: float | None = None,
) -> tuple[list[str], list[dict]]:
    day_seconds = 24 * 3600
    page_size = min(500, max_recent_trades, MAX_RECENT_TRADES_FAST)
    max_scan_rows = min(max_recent_trades, MAX_OFFSET, MAX_RECENT_TRADES_FAST)
    now_ts = int(time.time())
    since_ts = int(time.time()) - time_period_days * 24 * 3600
    wallet_volume: dict[str, float] = {}
    selected_trades: list[dict] = []
    scanned = 0
    started_at = started_at or time.time()

    for day_index in range(time_period_days):
        if time.time() - started_at > MAX_RUN_SECONDS:
            break
        window_end = now_ts - day_index * day_seconds
        window_start = max(since_ts, window_end - day_seconds)
        if window_end <= since_ts:
            break
        for offset in range(0, max_scan_rows, page_size):
            if time.time() - started_at > MAX_RUN_SECONDS:
                break
            if offset >= MAX_OFFSET:
                break
            page = cached_recent_trade_page(page_size, offset)
            if not page:
                continue
            page_timestamps = [int(trade.get("timestamp") or 0) for trade in page if trade.get("timestamp")]
            if page_timestamps and max(page_timestamps) < window_start:
                break
            for trade in page:
                scanned += 1
                if status:
                    status.write(f"Scanning trade {min(scanned, max_scan_rows)}/{max_scan_rows}")
                if progress:
                    progress.progress(min(scanned / max_scan_rows, 1.0))
                if len(selected_trades) >= max_scan_rows:
                    break
                timestamp = int(trade.get("timestamp") or 0)
                if not (window_start <= timestamp < window_end):
                    continue
                if not filter_items_by_period_and_category([trade], window_start, market_category):
                    continue
                wallet = str(trade.get("proxyWallet") or "").lower()
                if not wallet.startswith("0x") or len(wallet) != 42:
                    continue
                size = float(trade.get("size") or 0)
                price = float(trade.get("price") or 0)
                wallet_volume[wallet] = wallet_volume.get(wallet, 0.0) + abs(size * price)
                selected_trades.append(trade)
            if len(selected_trades) >= max_scan_rows:
                break
        if len(selected_trades) >= max_scan_rows:
            break
    return [
        wallet
        for wallet, _volume in sorted(wallet_volume.items(), key=lambda item: item[1], reverse=True)[:max_wallets]
    ], selected_trades


def calculate_fast_wallet_rows(recent_trades: list[dict], max_wallets: int) -> list[dict]:
    grouped: dict[str, dict] = {}
    for trade in recent_trades:
        wallet = str(trade.get("proxyWallet") or "").lower()
        if not wallet.startswith("0x") or len(wallet) != 42:
            continue
        size = float(trade.get("size") or 0)
        price = float(trade.get("price") or 0)
        volume = abs(size * price)
        item = grouped.setdefault(
            wallet,
            {
                "wallet": wallet,
                "trade_count": 0,
                "total_volume": 0.0,
                "trade_sizes": [],
                "markets": set(),
            },
        )
        item["trade_count"] += 1
        item["total_volume"] += volume
        item["trade_sizes"].append(volume)
        if trade.get("conditionId"):
            item["markets"].add(str(trade.get("conditionId")).lower())

    rows = []
    for wallet, item in grouped.items():
        resolved_markets = len(item["markets"])
        trade_count = int(item["trade_count"])
        total_volume = float(item["total_volume"])
        trade_sizes = item["trade_sizes"]
        avg_trade_size = total_volume / trade_count if trade_count else 0.0
        largest_trade = max(trade_sizes, default=0.0)
        same_size_ratio = repetitive_size_ratio(trade_sizes)
        adjusted_activity = (trade_count + 5) / (trade_count + 10) * 100
        volume_score = min(100.0, max(0.0, total_volume / 100))
        reliability = min(100.0, (trade_count / 25) * 100)
        final_score = adjusted_activity * 0.45 + volume_score * 0.35 + reliability * 0.20
        warning = bot_likeness_warning(
            trade_count,
            avg_trade_size,
            largest_trade,
            same_size_ratio,
            0.0,
            total_volume,
        )
        whale_score = (
            normalize_to_100(total_volume, 250000) * 0.35
            + normalize_to_100(avg_trade_size, 2500) * 0.25
            + normalize_to_100(largest_trade, 10000) * 0.25
            + adjusted_activity * 0.15
        )
        rows.append(
            {
                "wallet": wallet,
                "polygonscan_url": f"https://polygonscan.com/address/{wallet}",
                "polymarket_profile_url": f"https://polymarket.com/profile/{wallet}",
                "adjusted_win_rate": round(adjusted_activity, 2),
                "win_rate": None,
                "roi_pct": None,
                "net_profit": None,
                "total_pnl": 0.0,
                "resolved_markets": resolved_markets,
                "total_volume": round(total_volume, 2),
                "avg_trade_size": round(avg_trade_size, 2),
                "largest_trade": round(largest_trade, 2),
                "trade_liquidity_proxy": round(total_volume / max(resolved_markets, 1), 2),
                "unique_markets": resolved_markets,
                "recent_activity": trade_count,
                "liquidity_warning": "Liquidity data unavailable for this wallet.",
                "copyability_score": round(volume_score, 2),
                "final_score": round(final_score, 2),
                "whale_score": round(whale_score, 2),
                "bot_likeness_warning": warning,
                "fast_mode": True,
            }
        )
    return sorted(rows, key=lambda row: row["whale_score"], reverse=True)[:max_wallets]


def fetch_wallet_trade_history(wallet: str, time_period_days: int, market_category: str) -> tuple[list[dict], list[dict]]:
    closed = cached_closed_positions(wallet)
    trades = cached_wallet_trades(wallet)
    since_ts = int(time.time()) - time_period_days * 24 * 3600
    closed = filter_items_by_period_and_category(closed, since_ts, market_category)
    trades = filter_items_by_period_and_category(trades, since_ts, market_category)
    return closed, trades


def calculate_wallet_performance(
    wallet: str,
    time_period_days: int,
    market_category: str,
    include_timing: bool,
) -> dict:
    closed, trades = fetch_wallet_trade_history(wallet, time_period_days, market_category)

    price_history = {} if include_timing else None
    price_history_failed = False
    if include_timing:
        recent_assets = []
        for trade in sorted(trades, key=lambda t: int(t.get("timestamp") or 0), reverse=True):
            asset = str(trade.get("asset") or "")
            timestamp = int(trade.get("timestamp") or 0)
            if asset and timestamp and asset not in recent_assets:
                recent_assets.append(asset)
            if len(recent_assets) >= 20:
                break
        for asset in recent_assets:
            asset_trades = [t for t in trades if str(t.get("asset") or "") == asset and t.get("timestamp")]
            if not asset_trades:
                continue
            start = min(int(t["timestamp"]) for t in asset_trades) - 3600
            end = max(int(t["timestamp"]) for t in asset_trades) + 48 * 3600
            points, failed = fetch_price_history_chunked(asset, start, end)
            price_history[asset] = points
            price_history_failed = price_history_failed or failed
    if include_timing and (price_history_failed or not any(price_history.values())):
        price_history = None

    row = score_wallet(wallet, closed, trades, {}, price_history)
    row["price_history_warning"] = bool(include_timing and price_history_failed)
    save_wallet_score(db_connection(), wallet, row)
    return row


def analyze_wallet(wallet: str, include_timing: bool, time_period_days: int, market_category: str) -> dict:
    return calculate_wallet_performance(wallet, time_period_days, market_category, include_timing)


def market_link_for_trade(trade: dict) -> str:
    event_slug = str(trade.get("eventSlug") or "").strip()
    slug = str(trade.get("slug") or "").strip()
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return ""


def trade_identity(wallet: str, trade: dict) -> str:
    explicit_id = trade.get("id") or trade.get("tradeId") or trade.get("transactionHash")
    if explicit_id:
        return str(explicit_id)
    market_id = str(trade.get("conditionId") or trade.get("market") or trade.get("asset") or "").lower()
    timestamp = str(trade.get("timestamp") or "")
    side = str(trade.get("side") or "").lower()
    price = str(trade.get("price") or "")
    size = str(trade.get("size") or "")
    return f"{wallet.lower()}:{market_id}:{timestamp}:{side}:{price}:{size}"


def format_wallet_trades(wallet: str, trades: list[dict], resolved_condition_ids: set[str]) -> pd.DataFrame:
    rows = []
    for trade in trades:
        price = float(trade.get("price") or 0)
        size = float(trade.get("size") or 0)
        timestamp = int(trade.get("timestamp") or 0)
        condition_id = str(trade.get("conditionId") or "").lower()
        side = str(trade.get("side") or "").lower()
        outcome = str(trade.get("outcome") or "").upper()
        rows.append(
            {
                "wallet": wallet,
                "market_title": trade.get("title") or trade.get("slug") or "Unknown market",
                "outcome": outcome,
                "outcome_action": f"{side.title()} {outcome}" if side and outcome else outcome,
                "side": side,
                "price": price,
                "size_shares": size,
                "dollar_value": round(price * size, 2),
                "timestamp_raw": timestamp,
                "timestamp": pd.to_datetime(timestamp, unit="s", utc=True).strftime("%Y-%m-%d %H:%M UTC")
                if timestamp
                else "",
                "market_link": market_link_for_trade(trade),
                "market_status": "Resolved" if condition_id in resolved_condition_ids else "Open",
                "trade_id": trade_identity(wallet, trade),
            }
        )
    return pd.DataFrame(rows).sort_values("timestamp_raw", ascending=False) if rows else pd.DataFrame()


def load_wallet_trade_view(wallet: str) -> pd.DataFrame:
    trades = cached_wallet_recent_trades(wallet)
    try:
        closed_positions = cached_closed_positions(wallet)
    except PolymarketAPIError:
        closed_positions = []
    resolved_condition_ids = {
        str(position.get("conditionId") or "").lower()
        for position in closed_positions
        if position.get("conditionId")
    }
    return format_wallet_trades(wallet, trades, resolved_condition_ids)


def filter_trade_view(
    trades_df: pd.DataFrame,
    only_open: bool,
    only_buys: bool,
    min_trade_value: float,
    lookback_days: int,
) -> pd.DataFrame:
    if trades_df.empty:
        return trades_df
    filtered = trades_df.copy()
    cutoff = int(time.time()) - lookback_days * 24 * 3600
    filtered = filtered[filtered["timestamp_raw"] >= cutoff]
    if only_open:
        filtered = filtered[filtered["market_status"] == "Open"]
    if only_buys:
        filtered = filtered[filtered["side"].str.lower() == "buy"]
    filtered = filtered[filtered["dollar_value"] >= min_trade_value]
    return filtered.sort_values("timestamp_raw", ascending=False)


def latest_trade_time(wallet: str) -> str:
    try:
        trades = cached_wallet_recent_trades(wallet, limit=1)
    except PolymarketAPIError:
        return ""
    if not trades:
        return ""
    timestamp = int(trades[0].get("timestamp") or 0)
    return pd.to_datetime(timestamp, unit="s", utc=True).strftime("%Y-%m-%d %H:%M UTC") if timestamp else ""


def render_wallet_trade_viewer(wallet_options: list[str], selected_wallet: str | None = None) -> None:
    st.subheader("Wallet Trade Viewer")
    st.caption("Read-only recent trade viewer. No orders are placed and no wallet connection is used.")

    if wallet_options:
        viewer_wallet = st.selectbox(
            "Wallet",
            wallet_options,
            index=wallet_options.index(selected_wallet) if selected_wallet in wallet_options else 0,
            key="trade_viewer_wallet",
        )
    else:
        viewer_wallet = selected_wallet

    viewer_cols = st.columns([1, 1, 1, 1, 1])
    only_open = viewer_cols[0].checkbox("Only open markets", value=False)
    only_buys = viewer_cols[1].checkbox("Only buys", value=False)
    min_trade_value = viewer_cols[2].number_input("Minimum trade size ($)", min_value=0.0, value=0.0, step=50.0)
    lookback_label = viewer_cols[3].selectbox("Time window", ["Last 24h", "Last 7d", "Last 30d"], index=2)
    lookback_days = {"Last 24h": 1, "Last 7d": 7, "Last 30d": 30}[lookback_label]

    if not viewer_wallet:
        st.info("Select a ranked or watched wallet to view recent trades.")
        return

    add_disabled = viewer_wallet in st.session_state["watchlist"]
    if viewer_cols[4].button("Add to Watchlist", disabled=add_disabled):
        add_wallet_to_watchlist(viewer_wallet)
        st.rerun()
    try:
        trade_df = filter_trade_view(
            load_wallet_trade_view(viewer_wallet),
            only_open=only_open,
            only_buys=only_buys,
            min_trade_value=float(min_trade_value),
            lookback_days=lookback_days,
        )
    except PolymarketAPIError:
        logger.warning("Could not load trades for wallet %s", viewer_wallet, exc_info=True)
        trade_df = pd.DataFrame()
        st.warning("Recent trades unavailable for this wallet right now.")

    trade_columns = [
        "market_title",
        "outcome_action",
        "side",
        "price",
        "size_shares",
        "dollar_value",
        "timestamp",
        "market_link",
        "market_status",
    ]
    if trade_df.empty:
        st.info("No trades match the current filters.")
        return
    st.dataframe(
        trade_df[trade_columns],
        use_container_width=True,
        hide_index=True,
        column_config={
            "market_title": "Market",
            "outcome_action": "YES/NO",
            "side": "Buy/sell",
            "price": st.column_config.NumberColumn("Price", format="%.4f"),
            "size_shares": st.column_config.NumberColumn("Size", format="%.2f"),
            "dollar_value": st.column_config.NumberColumn("Dollar value", format="$%.2f"),
            "timestamp": "Timestamp",
            "market_link": st.column_config.LinkColumn("Market link", display_text="Open"),
            "market_status": "Market status",
        },
    )


def collect_watchlist_trades(whale_wallets: set[str]) -> pd.DataFrame:
    watchlist_frames = []
    for wallet in st.session_state["watchlist"]:
        try:
            wallet_trades = load_wallet_trade_view(wallet)
        except PolymarketAPIError:
            logger.warning("Could not refresh watchlist wallet %s", wallet, exc_info=True)
            continue
        if wallet_trades.empty:
            continue
        wallet_trades = filter_trade_view(
            wallet_trades,
            only_open=False,
            only_buys=False,
            min_trade_value=0.0,
            lookback_days=30,
        )
        wallet_trades["whale_wallet"] = wallet in whale_wallets
        watchlist_frames.append(wallet_trades)
    if not watchlist_frames:
        return pd.DataFrame()
    return pd.concat(watchlist_frames, ignore_index=True).sort_values("timestamp_raw", ascending=False)


def record_seen_trade(seen_ids: set[str], seen_timestamps: dict[str, int], row: pd.Series) -> None:
    trade_id = str(row.get("trade_id") or "")
    if not trade_id:
        return
    seen_ids.add(trade_id)
    seen_timestamps[trade_id] = int(row.get("timestamp_raw") or 0)


def alert_toast_message(alert: dict) -> str:
    side = str(alert.get("side") or "").lower()
    verb = "bought" if side == "buy" else "sold"
    price = alert.get("price")
    price_label = f"{float(price):.4f}" if price not in (None, "") else "unknown price"
    return (
        f"New whale trade: {alert.get('wallet')} {verb} {alert.get('outcome') or 'UNKNOWN'} "
        f"on {alert.get('market') or 'Unknown market'} at {price_label}"
    )


def render_optional_alert_sound() -> None:
    components.html(
        """
        <script>
        try {
          const AudioContext = window.AudioContext || window.webkitAudioContext;
          const context = new AudioContext();
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          oscillator.type = "sine";
          oscillator.frequency.value = 660;
          gain.gain.value = 0.035;
          oscillator.connect(gain);
          gain.connect(context.destination);
          oscillator.start();
          setTimeout(() => {
            oscillator.stop();
            context.close();
          }, 160);
        } catch (error) {}
        </script>
        """,
        height=0,
    )


def detect_watchlist_alerts(
    watchlist_df: pd.DataFrame,
    alert_min_trade_value: float,
    enable_popup_alerts: bool,
    play_sound: bool,
) -> None:
    seen_ids = {str(item) for item in st.session_state.get("seen_trade_ids", [])}
    seen_timestamps = {
        str(key): int(value or 0)
        for key, value in st.session_state.get("seen_trade_timestamps", {}).items()
    }
    initialized_wallets = {
        str(wallet).lower()
        for wallet in st.session_state.get("watchlist_alert_wallets_initialized", [])
    }

    if watchlist_df.empty:
        initialized_wallets.update(st.session_state["watchlist"])
        st.session_state["watchlist_alert_wallets_initialized"] = sorted(initialized_wallets)
        return

    new_alerts = []
    wallet_series = watchlist_df["wallet"].astype(str).str.lower()
    for wallet in st.session_state["watchlist"]:
        wallet_rows = watchlist_df[wallet_series == wallet]
        if wallet not in initialized_wallets:
            for _, row in wallet_rows.iterrows():
                record_seen_trade(seen_ids, seen_timestamps, row)
            initialized_wallets.add(wallet)
            continue

        unseen_rows = wallet_rows[~wallet_rows["trade_id"].astype(str).isin(seen_ids)]
        eligible_rows = unseen_rows[unseen_rows["dollar_value"] >= float(alert_min_trade_value)]
        for _, row in eligible_rows.sort_values("timestamp_raw", ascending=False).iterrows():
            alert = {
                "wallet": wallet,
                "market": row.get("market_title") or "Unknown market",
                "outcome": row.get("outcome") or "",
                "side": row.get("side") or "",
                "price": row.get("price"),
                "size": row.get("size_shares"),
                "dollar_value": row.get("dollar_value"),
                "timestamp": row.get("timestamp") or "",
                "market_link": row.get("market_link") or "",
                "trade_id": str(row.get("trade_id") or ""),
                "whale_wallet": bool(row.get("whale_wallet")),
            }
            new_alerts.append(alert)
            if enable_popup_alerts:
                st.toast(alert_toast_message(alert))

        for _, row in wallet_rows.iterrows():
            record_seen_trade(seen_ids, seen_timestamps, row)

    st.session_state["seen_trade_ids"] = sorted(seen_ids)
    st.session_state["seen_trade_timestamps"] = seen_timestamps
    st.session_state["watchlist_alert_wallets_initialized"] = sorted(initialized_wallets)
    if new_alerts:
        st.session_state["new_trade_alerts"] = (new_alerts + st.session_state.get("new_trade_alerts", []))[:50]
        if play_sound:
            render_optional_alert_sound()


def render_new_alerts() -> None:
    st.subheader("New alerts")
    alerts = st.session_state.get("new_trade_alerts", [])
    if not alerts:
        st.caption("No new alerts yet. New watched-wallet trades above the alert minimum will appear here.")
        return
    clear_col, count_col = st.columns([1, 4])
    if clear_col.button("Clear alerts", key="clear-watchlist-alerts"):
        st.session_state["new_trade_alerts"] = []
        st.rerun()
    count_col.caption(f"{len(alerts)} recent alert{'s' if len(alerts) != 1 else ''}")
    alert_df = pd.DataFrame(alerts)
    alert_columns = ["wallet", "market", "outcome", "side", "price", "size", "dollar_value", "timestamp", "market_link"]
    st.dataframe(
        alert_df[alert_columns],
        use_container_width=True,
        hide_index=True,
        column_config={
            "wallet": "Wallet",
            "market": "Market",
            "outcome": "YES/NO",
            "side": "Buy/sell",
            "price": st.column_config.NumberColumn("Price", format="%.4f"),
            "size": st.column_config.NumberColumn("Size", format="%.2f"),
            "dollar_value": st.column_config.NumberColumn("Dollar value", format="$%.2f"),
            "timestamp": "Timestamp",
            "market_link": st.column_config.LinkColumn("Market link", display_text="Open"),
        },
    )


def last_trade_times_from_df(watchlist_df: pd.DataFrame) -> dict[str, str]:
    if watchlist_df.empty:
        return {}
    last_trade_times = {}
    for wallet, group in watchlist_df.groupby("wallet"):
        latest_timestamp = int(group["timestamp_raw"].max() or 0)
        last_trade_times[str(wallet).lower()] = (
            pd.to_datetime(latest_timestamp, unit="s", utc=True).strftime("%Y-%m-%d %H:%M UTC")
            if latest_timestamp
            else ""
        )
    return last_trade_times


def render_watchlist_body(
    ranked_lookup: dict[str, dict],
    whale_mode: bool,
    alert_min_trade_value: float,
    enable_popup_alerts: bool,
    play_sound: bool,
) -> None:
    whale_wallets = set(ranked_lookup.keys()) if whale_mode else set()
    watchlist_df = collect_watchlist_trades(whale_wallets)
    detect_watchlist_alerts(watchlist_df, alert_min_trade_value, enable_popup_alerts, play_sound)
    render_new_alerts()

    st.divider()
    st.caption("Saved wallets")
    header = st.columns([4, 1, 1, 1, 2, 1])
    header[0].markdown("**Wallet address**")
    header[1].markdown("**Whale score**")
    header[2].markdown("**Net profit**")
    header[3].markdown("**ROI**")
    header[4].markdown("**Last trade time**")
    header[5].markdown("**Remove**")

    last_trade_times = last_trade_times_from_df(watchlist_df)
    for item in list(st.session_state["watchlist_items"]):
        wallet = str(item.get("wallet", "")).lower()
        merged = {**item, **ranked_lookup.get(wallet, {})}
        last_trade = last_trade_times.get(wallet, "")
        cols = st.columns([4, 1, 1, 1, 2, 1])
        cols[0].code(wallet, language="text")
        cols[1].write("" if merged.get("whale_score") is None else f"{float(merged.get('whale_score')):.2f}")
        cols[2].write("" if merged.get("net_profit") is None else f"${float(merged.get('net_profit')):,.2f}")
        cols[3].write("" if merged.get("roi_pct") is None else f"{float(merged.get('roi_pct')):.2f}%")
        cols[4].write(last_trade or "N/A")
        if cols[5].button("Remove", key=f"remove-{wallet}"):
            remove_wallet_from_watchlist(wallet)
            st.rerun()

    st.divider()
    st.subheader("Recent Watchlist Trades")
    if watchlist_df.empty:
        st.info("No recent watchlist trades found yet.")
        return

    watchlist_columns = [
        "wallet",
        "market_title",
        "outcome",
        "side",
        "price",
        "size_shares",
        "timestamp",
        "market_link",
    ]

    def highlight_whales(row):
        style = "background-color: rgba(255, 215, 0, 0.22)" if row.get("whale_wallet") else ""
        return [style for _ in row]

    st.dataframe(
        watchlist_df[watchlist_columns + ["whale_wallet"]].style.apply(highlight_whales, axis=1),
        use_container_width=True,
        hide_index=True,
        column_config={
            "wallet": "Wallet",
            "market_title": "Market",
            "outcome": "YES/NO",
            "side": "Buy/sell",
            "price": st.column_config.NumberColumn("Price", format="%.4f"),
            "size_shares": st.column_config.NumberColumn("Size", format="%.2f"),
            "timestamp": "Timestamp",
            "market_link": st.column_config.LinkColumn("Market link", display_text="Open"),
            "whale_wallet": "Whale wallet",
        },
    )


def render_watchlist_page(ranked_df: pd.DataFrame | None = None, whale_mode: bool = True) -> None:
    st.subheader("Watchlist")
    ensure_watchlist_state()
    watchlist_items = st.session_state["watchlist_items"]
    settings_state = user_settings()

    controls = st.columns([1, 1, 1, 1])
    auto_refresh = controls[0].checkbox("Auto-refresh Watchlist", value=True)
    refresh_seconds = controls[1].selectbox(
        "Refresh interval",
        [30, 60],
        index=0,
        format_func=lambda seconds: f"{seconds} seconds",
    )
    enable_popup_alerts = controls[2].checkbox(
        "Enable popup alerts",
        value=bool(settings_state["popup_alerts_enabled"]),
    )
    alert_min_trade_value = controls[3].number_input(
        "Alert minimum trade size ($)",
        min_value=0.0,
        value=float(settings_state["min_trade_size"]),
        step=50.0,
    )
    update_user_settings(
        min_trade_size=float(alert_min_trade_value),
        popup_alerts_enabled=bool(enable_popup_alerts),
    )
    play_sound = st.checkbox("Play subtle sound", value=False)

    if not watchlist_items:
        st.info("No wallets in watchlist yet. Add wallets from the dashboard.")
        return

    if st.button("Refresh Watchlist Trades"):
        cached_wallet_recent_trades.clear()
        sync_user_data_from_supabase(force=True)
        st.rerun()

    ranked_lookup = {}
    if ranked_df is not None and not ranked_df.empty and "wallet" in ranked_df.columns:
        ranked_lookup = {str(row["wallet"]).lower(): row.to_dict() for _, row in ranked_df.iterrows()}

    if auto_refresh and hasattr(st, "fragment"):
        st.caption(f"Auto-refreshing every {int(refresh_seconds)} seconds.")
        st.fragment(run_every=int(refresh_seconds))(render_watchlist_body)(
            ranked_lookup,
            whale_mode,
            float(alert_min_trade_value),
            enable_popup_alerts,
            play_sound,
        )
    else:
        if auto_refresh:
            components.html(
                f"<script>setTimeout(() => window.parent.location.reload(), {int(refresh_seconds) * 1000});</script>",
                height=0,
            )
            st.caption(f"Auto-refreshing every {int(refresh_seconds)} seconds.")
        render_watchlist_body(
            ranked_lookup,
            whale_mode,
            float(alert_min_trade_value),
            enable_popup_alerts,
            play_sound,
        )


if not current_auth_session():
    render_auth_page()
    st.stop()

sync_user_data_from_supabase()
auth_session = current_auth_session()
settings_state = user_settings()

st.title("Poly Radar")
st.caption("Ranks public wallets for research only. This app never places trades or asks for private keys.")

with st.sidebar:
    st.caption(f"Logged in as {auth_session.get('email', 'unknown') if auth_session else 'unknown'}")
    if st.button("Logout"):
        sign_out()
    page = st.radio("Page", ["Dashboard", "Wallet Details", "Watchlist"])
    st.header("Discovery")
    whale_mode = st.checkbox("Whale Mode", value=bool(settings_state["whale_mode_enabled"]))
    update_user_settings(whale_mode_enabled=bool(whale_mode))
    fast_mode = st.checkbox("Fast Mode", value=not whale_mode)
    market_category = st.selectbox("Market category", list(CATEGORY_KEYWORDS.keys()))
    time_period_days = st.selectbox("Time period", [30, 90, 180], index=1, format_func=lambda d: f"Last {d} days")
    max_recent_trades = st.number_input(
        "Recent trades to scan",
        min_value=500,
        max_value=MAX_RECENT_TRADES_FAST,
        value=MAX_RECENT_TRADES_FAST,
        step=100,
    )
    max_wallets = st.number_input(
        "Candidate wallets to analyze",
        min_value=5,
        max_value=MAX_WALLETS_FAST,
        value=MAX_WALLETS_FAST,
        step=5,
    )
    discover = st.button("Discover Wallets", type="primary")
    st.header("Manual Wallets")
    wallet_text = st.text_area(
        "Optional wallet addresses",
        height=110,
        placeholder="0x...\n0x...",
    )
    st.header("Filters")
    if whale_mode:
        min_net_profit = st.slider("Minimum net profit", 0, 100000, 2000, 500)
        min_volume = st.slider("Minimum total volume", 0, 1000000, 25000, 5000)
        min_avg_trade_size = st.slider("Minimum average trade size", 0, 10000, 250, 50)
        min_largest_trade = st.slider("Minimum largest trade", 0, 100000, 1000, 500)
        min_resolved = st.slider("Minimum resolved markets", 0, 250, 50, 5)
    else:
        min_net_profit = 0
        min_avg_trade_size = 0
        min_largest_trade = 0
        min_resolved = st.number_input("Minimum resolved markets", min_value=0, value=5 if fast_mode else 50, step=5)
        min_volume = st.number_input("Minimum total volume", min_value=0.0, value=100.0 if fast_mode else 1000.0, step=250.0)
    min_win_rate = st.number_input("Minimum win rate %", min_value=0.0, max_value=100.0, value=50.0, step=1.0)
    min_roi = st.number_input("Minimum ROI %", min_value=-100.0, value=0.0, step=1.0)
    min_unique_markets = st.number_input("Minimum unique markets", min_value=0, value=0, step=1)
    exclude_lucky = st.checkbox("Exclude one lucky big win", value=True)
    exclude_low_liq = st.checkbox("Exclude weak trade-data liquidity", value=True)
    include_timing = st.checkbox("Estimate entry timing from price history", value=False, disabled=fast_mode)
    analyze = st.button("Analyze manual wallets")

wallets = normalize_wallets(wallet_text)

if discover:
    try:
        run_started_at = time.time()
        scan_status = st.empty()
        scan_progress = st.progress(0, "Scanning recent trades...")
        wallets, recent_trades = discover_wallets_from_recent_trades(
            int(time_period_days),
            str(market_category),
            int(max_recent_trades),
            int(max_wallets),
            progress=scan_progress,
            status=scan_status,
            started_at=run_started_at,
        )
        st.session_state["discovered_wallets"] = wallets
        st.session_state["recent_trades"] = recent_trades
        st.success("Fetched recent trades successfully.")
        if fast_mode:
            rows = calculate_fast_wallet_rows(recent_trades, int(max_wallets))
            st.session_state["rows"] = rows
            st.session_state["fast_mode_results"] = True
            if whale_mode:
                st.info("Fast Mode used recent trades only. These are whale candidates; switch off Fast Mode for net profit and resolved-market filters.")
            else:
                st.info("Fast Mode used recent trades only. Results are rough but quick.")
    except PolymarketAPIError as exc:
        logger.warning("Could not discover wallets", exc_info=exc)
        st.error("Could not discover wallets from public API data right now.")
        wallets = []

if analyze and wallets:
    st.session_state["discovered_wallets"] = wallets

candidates = st.session_state.get("discovered_wallets", wallets)[: int(max_wallets)]
st.caption(f"Analyzing {len(candidates)} candidate wallets")

if (discover and not fast_mode) or analyze:
    rows = []
    price_history_warning = False
    progress = st.progress(0, "Fetching public Polymarket data...")
    status = st.empty()
    run_started_at = time.time()
    analyzed_wallets = candidates[: int(max_wallets)]
    for index, wallet in enumerate(analyzed_wallets, start=1):
        if index > MAX_API_CALLS_PER_RUN or time.time() - run_started_at > MAX_RUN_SECONDS:
            st.info("Run limit reached. Showing partial results.")
            break
        status.write(f"Analyzing wallet {index}/{len(analyzed_wallets)}")
        try:
            row = analyze_wallet(wallet, include_timing, int(time_period_days), str(market_category))
            price_history_warning = price_history_warning or bool(row.get("price_history_warning"))
            rows.append(row)
        except PolymarketAPIError as exc:
            logger.warning("Skipped wallet %s", wallet, exc_info=exc)
            st.warning(f"{wallet}: public API data unavailable; skipped.")
        progress.progress(index / len(analyzed_wallets), f"Analyzing wallet {index}/{len(analyzed_wallets)}")
    st.session_state["rows"] = rows
    st.session_state["fast_mode_results"] = False
    st.session_state["price_history_warning"] = price_history_warning

rows = st.session_state.get("rows", [])
if not rows:
    if page == "Watchlist":
        render_watchlist_page()
        st.stop()
    if page == "Wallet Details":
        render_wallet_trade_viewer(st.session_state["watchlist"], st.session_state.get("selected_wallet"))
        st.stop()
    st.info("Click Discover Wallets to scan recent public trades, or paste wallets and click Analyze manual wallets.")
    st.stop()

if st.session_state.get("price_history_warning"):
    st.warning("Some price history unavailable; entry timing estimate skipped.")

if any(row.get("liquidity_warning") for row in rows):
    st.warning("Liquidity data unavailable for this wallet.")

settings = FilterSettings(
    min_resolved_trades=int(min_resolved),
    min_unique_markets=int(min_unique_markets),
    min_win_rate=float(min_win_rate),
    min_roi=float(min_roi),
    min_total_volume=float(min_volume),
    market_category=str(market_category),
    time_period_days=int(time_period_days),
    exclude_lucky_big_win=exclude_lucky,
    exclude_low_liquidity_trades=exclude_low_liq,
)

if whale_mode:
    required_profit = max(float(min_net_profit), 1000.0)
    filtered = []
    for row in rows:
        warning = str(row.get("bot_likeness_warning") or "")
        if st.session_state.get("fast_mode_results"):
            if (row.get("total_volume") or 0) < float(min_volume):
                continue
            if (row.get("avg_trade_size") or 0) < float(min_avg_trade_size):
                continue
            if (row.get("largest_trade") or 0) < float(min_largest_trade):
                continue
        else:
            if (row.get("net_profit") or 0) < required_profit:
                continue
            if (row.get("total_volume") or 0) < float(min_volume):
                continue
            if (row.get("avg_trade_size") or 0) < float(min_avg_trade_size):
                continue
            if (row.get("largest_trade") or 0) < float(min_largest_trade):
                continue
            if (row.get("resolved_markets") or 0) < int(min_resolved):
                continue
            if (row.get("lucky_big_win_share") or 0) > 0.45:
                continue
        if "Tiny average trade size" in warning:
            continue
        if "Repetitive same-size trades" in warning:
            continue
        if "High trade count with low average size" in warning:
            continue
        filtered.append(row)
    filtered = sorted(filtered, key=lambda item: item.get("whale_score", 0), reverse=True)
elif st.session_state.get("fast_mode_results"):
    filtered = [
        row for row in rows
        if row["total_volume"] >= float(min_volume) and row["resolved_markets"] >= int(min_resolved)
    ]
    filtered = sorted(filtered, key=lambda item: item["final_score"], reverse=True)
else:
    filtered = apply_filters(rows, settings)
df = pd.DataFrame(filtered)

if page == "Watchlist":
    render_watchlist_page(df if not df.empty else pd.DataFrame(rows), whale_mode)
    st.stop()

if page == "Wallet Details":
    if "wallet" in df.columns:
        detail_wallets = [str(wallet) for wallet in df["wallet"].dropna().tolist()]
    else:
        detail_wallets = []
    detail_wallets = list(dict.fromkeys(detail_wallets + st.session_state["watchlist"]))
    render_wallet_trade_viewer(detail_wallets, st.session_state.get("selected_wallet"))
    st.stop()

top_cols = st.columns(4)
top_cols[0].metric("Wallets analyzed", len(rows))
top_cols[1].metric("Wallets passing filters", len(filtered))
score_column = "whale_score" if whale_mode else "final_score"
if score_column not in df.columns:
    df[score_column] = 0
top_cols[2].metric("Best score", f"{df[score_column].max():.2f}" if not df.empty else "N/A")
roi_values = pd.to_numeric(df["roi_pct"], errors="coerce").dropna() if not df.empty else pd.Series(dtype=float)
top_cols[3].metric("Median ROI", f"{roi_values.median():.2f}%" if not roi_values.empty else "N/A")

if df.empty:
    st.warning("No wallets passed the current filters. Lower the thresholds or include more wallets.")
    st.stop()

if whale_mode:
    columns = [
        "wallet",
        "net_profit",
        "roi_pct",
        "win_rate",
        "adjusted_win_rate",
        "total_volume",
        "avg_trade_size",
        "largest_trade",
        "resolved_markets",
        "whale_score",
        "bot_likeness_warning",
    ]
else:
    columns = [
        "wallet",
        "polygonscan_url",
        "polymarket_profile_url",
        "adjusted_win_rate",
        "win_rate",
        "roi_pct",
        "net_profit",
        "resolved_markets",
        "total_volume",
        "trade_liquidity_proxy",
        "unique_markets",
        "recent_activity",
        "copyability_score",
        "final_score",
    ]
for column in columns:
    if column not in df.columns:
        df[column] = None
st.caption("Click a wallet row to open its recent trade viewer. This is read-only.")
ranking_event = st.dataframe(
    df[columns],
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="wallet_rankings_table",
    column_config={
        "wallet": "Wallet address",
        "polygonscan_url": st.column_config.LinkColumn("Polygonscan", display_text="Open"),
        "polymarket_profile_url": st.column_config.LinkColumn("Polymarket", display_text="Open"),
        "adjusted_win_rate": st.column_config.NumberColumn("Adjusted win rate", format="%.2f%%"),
        "final_score": st.column_config.NumberColumn("Score", format="%.2f"),
        "whale_score": st.column_config.NumberColumn("Whale score", format="%.2f"),
        "net_profit": st.column_config.NumberColumn("Net profit", format="$%.2f"),
        "roi_pct": st.column_config.NumberColumn("ROI %", format="%.2f%%"),
        "win_rate": st.column_config.NumberColumn("Win rate", format="%.2f%%"),
        "total_volume": st.column_config.NumberColumn("Total traded volume", format="$%.2f"),
        "avg_trade_size": st.column_config.NumberColumn("Average trade size", format="$%.2f"),
        "largest_trade": st.column_config.NumberColumn("Largest trade", format="$%.2f"),
        "trade_liquidity_proxy": st.column_config.NumberColumn("Trade liquidity proxy", format="$%.2f"),
        "unique_markets": st.column_config.NumberColumn("Unique markets", format="%d"),
        "recent_activity": st.column_config.NumberColumn("Recent activity", format="%d"),
        "bot_likeness_warning": "Bot-likeness warning",
    },
)

selected_wallet = None
if isinstance(ranking_event, dict):
    selected_rows = ranking_event.get("selection", {}).get("rows", [])
else:
    selected_rows = getattr(getattr(ranking_event, "selection", None), "rows", [])
if selected_rows:
    selected_wallet = str(df.iloc[selected_rows[0]]["wallet"])
    st.session_state["selected_wallet"] = selected_wallet
else:
    selected_wallet = st.session_state.get("selected_wallet")

if selected_wallet:
    selected_row = df[df["wallet"].astype(str) == selected_wallet]
    selected_metadata = selected_row.iloc[0].to_dict() if not selected_row.empty else None
    action_cols = st.columns([3, 1])
    action_cols[0].caption(f"Selected wallet: {selected_wallet}")
    if action_cols[1].button(
        "Add to Watchlist",
        disabled=selected_wallet in st.session_state["watchlist"],
        key="add-selected-wallet",
    ):
        add_wallet_to_watchlist(selected_wallet, selected_metadata)
        st.rerun()

csv = df[columns].to_csv(index=False).encode("utf-8")
st.download_button("Export results to CSV", csv, "polymarket_wallet_rankings.csv", "text/csv")

with st.expander("How the score works"):
    st.markdown(
        """
        Whale Mode uses normalized 0-100 inputs so large dollar values do not overpower everything:

        `Net Profit x 0.30 + Total Volume x 0.20 + ROI x 0.15 + Adjusted Win Rate x 0.15 + Average Trade Size x 0.10 + Copyability x 0.10`

        Standard mode uses this reliability-adjusted ranking:

        `Adjusted Win Rate x 0.40 + ROI x 0.20 + Copyability x 0.20 + Trade Liquidity Quality x 0.10 + Trade Count Reliability x 0.10`

        Adjusted Win Rate is `(wallet wins + 5) / (wallet resolved markets + 10)`, which prevents tiny samples from ranking first.
        ROI is normalized from profit relative to amount bought. Copyability rewards smoother drawdowns, avoids wallets
        dominated by one huge win, estimates whether entries were followed by favorable 24-hour price movement, and
        mildly penalizes very large average trade sizes. Liquidity quality uses stable trade-data proxies: total volume,
        average trade size, unique markets, recent activity, and resolved market count.
        """
    )
