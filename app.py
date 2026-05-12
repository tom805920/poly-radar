from __future__ import annotations

import base64
import time
import logging
import html
import math
from dataclasses import dataclass
from pathlib import Path

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
from polymarket_tracker.crypto import (
    CHAIN_CONFIGS,
    CryptoAPIError,
    calculate_crypto_wallet_score,
    discover_crypto_whales,
    fetch_crypto_recent_trades,
    fetch_crypto_token_balances,
    fetch_crypto_wallet_activity,
)
from polymarket_tracker.metrics import (
    CATEGORY_KEYWORDS,
    FilterSettings,
    apply_filters,
    bot_likeness_penalty,
    bot_likeness_warning,
    filter_items_by_period_and_category,
    normalize_wallets,
    normalize_to_100,
    repetitive_size_ratio,
    score_wallet,
    whale_tier,
)
from polymarket_tracker.firebase_store import (
    DEFAULT_USER_SETTINGS,
    FIREBASE_CONNECTION_ERROR,
    FirebaseError,
    FirebaseStore,
)


APP_NAME = "WhaleWatch"
APP_TAGLINE = "Track smart money across prediction markets."
APP_ROOT = Path(__file__).resolve().parent
LOGO_IMAGE_PATH = APP_ROOT / "assets" / "whalewatch-logo.png"

st.set_page_config(page_title=APP_NAME, layout="wide")
logger = logging.getLogger(__name__)

DEFAULT_RECENT_TRADES_TO_SCAN = 1000
MAX_WALLETS_FAST = 50
DEFAULT_MAX_API_CALLS_PER_RUN = 300
DEFAULT_MAX_RUN_SECONDS = 180


@st.cache_data(show_spinner=False)
def logo_data_uri(path: str, modified_ns: int) -> str:
    logo_path = Path(path)
    if not logo_path.exists():
        return ""
    encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def inject_whalewatch_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

        :root {
            --ww-bg: #05070d;
            --ww-bg-2: #080d16;
            --ww-panel: rgba(11, 17, 29, 0.92);
            --ww-panel-2: rgba(14, 23, 38, 0.92);
            --ww-border: rgba(116, 147, 185, 0.18);
            --ww-border-strong: rgba(37, 179, 255, 0.36);
            --ww-text: #edf6ff;
            --ww-muted: #8ea1b7;
            --ww-blue: #19b8ff;
            --ww-blue-soft: rgba(25, 184, 255, 0.13);
            --ww-green: #20f29c;
            --ww-green-soft: rgba(32, 242, 156, 0.12);
            --ww-red: #ff526d;
            --ww-red-soft: rgba(255, 82, 109, 0.12);
            --ww-amber: #ffd166;
            --ww-shadow: 0 22px 70px rgba(0, 0, 0, 0.45);
        }

        html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
            background:
                radial-gradient(circle at 15% 0%, rgba(25, 184, 255, 0.12), transparent 28rem),
                radial-gradient(circle at 85% 12%, rgba(32, 242, 156, 0.08), transparent 24rem),
                linear-gradient(135deg, #03050a 0%, #07101d 52%, #05070d 100%) !important;
            color: var(--ww-text) !important;
            font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }

        .block-container {
            max-width: 1500px;
            padding-top: 1.15rem;
            padding-bottom: 4rem;
        }

        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, rgba(5, 9, 16, 0.98), rgba(9, 15, 27, 0.96)) !important;
            border-right: 1px solid var(--ww-border);
            box-shadow: 18px 0 60px rgba(0, 0, 0, 0.28);
        }

        [data-testid="stSidebar"] * {
            color: var(--ww-text);
        }

        [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3, [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            letter-spacing: 0;
        }

        h1, h2, h3 {
            letter-spacing: 0 !important;
        }

        .ww-topbar {
            position: sticky;
            top: 0;
            z-index: 99;
            margin: -0.25rem 0 1.25rem;
            padding: 1rem 1.15rem;
            border: 1px solid var(--ww-border);
            background: linear-gradient(135deg, rgba(5, 9, 16, 0.92), rgba(12, 20, 34, 0.86));
            backdrop-filter: blur(18px);
            box-shadow: var(--ww-shadow);
            border-radius: 8px;
        }

        .ww-auth-shell {
            min-height: 76vh;
            display: flex;
            align-items: center;
        }

        .ww-auth-card, .ww-panel, .ww-section {
            border: 1px solid var(--ww-border);
            background: linear-gradient(145deg, var(--ww-panel), rgba(7, 12, 21, 0.88));
            box-shadow: var(--ww-shadow);
            border-radius: 8px;
        }

        .ww-auth-card {
            padding: 2rem;
            margin: 1rem 0 1.25rem;
        }

        .ww-brand-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
        }

        .ww-logo {
            display: inline-flex;
            align-items: center;
            gap: 0.65rem;
            font-weight: 900;
            font-size: clamp(2.2rem, 5vw, 4.35rem);
            line-height: 0.94;
            color: var(--ww-text);
            text-transform: none;
        }

        .ww-logo-mark {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1.56em;
            min-width: 1.56em;
            height: 1.56em;
            border: 0;
            box-shadow: none;
            border-radius: 0;
            color: var(--ww-blue);
            font-size: 1em;
            letter-spacing: 0;
            overflow: visible;
            background: transparent;
        }

        .ww-logo-mark img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
            filter: brightness(0) invert(1) drop-shadow(0 0 18px rgba(237, 246, 255, 0.18));
        }

        .ww-logo-small {
            font-size: 1.55rem;
            letter-spacing: 0;
        }

        .ww-tagline {
            color: var(--ww-muted);
            font-size: 1.02rem;
            margin-top: 0.55rem;
        }

        .ww-terminal-line {
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--ww-blue), var(--ww-green), transparent);
            opacity: 0.72;
            margin: 1rem 0 0;
        }

        .ww-pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
        }

        .ww-badge, .ww-user-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.42rem;
            min-height: 1.9rem;
            border: 1px solid var(--ww-border);
            background: rgba(255, 255, 255, 0.035);
            color: var(--ww-muted);
            padding: 0.38rem 0.65rem;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            border-radius: 6px;
        }

        .ww-badge-blue {
            color: var(--ww-blue);
            border-color: rgba(25, 184, 255, 0.34);
            background: var(--ww-blue-soft);
        }

        .ww-badge-green {
            color: var(--ww-green);
            border-color: rgba(32, 242, 156, 0.34);
            background: var(--ww-green-soft);
        }

        .ww-badge-red {
            color: var(--ww-red);
            border-color: rgba(255, 82, 109, 0.34);
            background: var(--ww-red-soft);
        }

        .ww-tier {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 5.7rem;
            padding: 0.24rem 0.48rem;
            border: 1px solid var(--ww-border);
            font-weight: 900;
            border-radius: 6px;
            text-transform: uppercase;
            font-size: 0.72rem;
        }

        .ww-tier-kraken {
            color: #ff526d;
            border-color: rgba(255, 82, 109, 0.58);
            background: rgba(255, 82, 109, 0.11);
            box-shadow: 0 0 22px rgba(255, 82, 109, 0.28);
        }

        .ww-tier-leviathan {
            color: #b784ff;
            border-color: rgba(183, 132, 255, 0.58);
            background: rgba(183, 132, 255, 0.11);
            box-shadow: 0 0 22px rgba(183, 132, 255, 0.26);
        }

        .ww-tier-blue-whale {
            color: var(--ww-blue);
            border-color: rgba(25, 184, 255, 0.46);
            background: rgba(25, 184, 255, 0.10);
            box-shadow: 0 0 16px rgba(25, 184, 255, 0.18);
        }

        .ww-tier-shark {
            color: var(--ww-green);
            border-color: rgba(32, 242, 156, 0.34);
            background: rgba(32, 242, 156, 0.08);
            box-shadow: 0 0 10px rgba(32, 242, 156, 0.10);
        }

        .ww-tier-dolphin {
            color: #9aa8b8;
            border-color: rgba(154, 168, 184, 0.22);
            background: rgba(154, 168, 184, 0.05);
        }

        .ww-wallet-panel {
            border: 1px solid var(--ww-border);
            background: linear-gradient(145deg, rgba(10, 17, 30, 0.95), rgba(4, 8, 15, 0.92));
            box-shadow: var(--ww-shadow);
            border-radius: 8px;
            padding: 1rem;
            margin-top: 1rem;
        }

        .ww-wallet-address {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            color: var(--ww-text);
            word-break: break-all;
            font-size: 0.92rem;
        }

        .ww-wallet-address.compact {
            font-size: 0.78rem;
            color: #dbeafe;
        }

        .ww-wallet-inline {
            display: inline-flex;
            align-items: center;
            gap: 0.55rem;
            min-width: 0;
        }

        .ww-compact-title-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: 0.65rem 0 0.6rem;
        }

        .ww-compact-title {
            color: var(--ww-text);
            font-size: clamp(1.25rem, 2vw, 1.75rem);
            font-weight: 900;
            letter-spacing: 0;
        }

        .ww-watchlist-head {
            color: var(--ww-muted);
            font-size: 0.72rem;
            font-weight: 900;
            text-transform: uppercase;
            border-bottom: 1px solid var(--ww-border);
            padding-bottom: 0.25rem;
            margin-top: 0.25rem;
        }

        .ww-alert-row {
            border: 1px solid var(--ww-border);
            background: rgba(10, 17, 30, 0.74);
            border-radius: 8px;
            padding: 0.65rem 0.75rem;
            margin: 0.45rem 0;
        }

        .ww-alert-market {
            color: var(--ww-text);
            font-weight: 800;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .ww-wallet-table-wrap {
            width: 100%;
            overflow-x: auto;
            border: 1px solid var(--ww-border);
            background: linear-gradient(145deg, rgba(8, 14, 25, 0.96), rgba(4, 8, 15, 0.94));
            box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
            border-radius: 8px;
        }

        .ww-wallet-table {
            min-width: 1240px;
        }

        .ww-wallet-row {
            display: grid;
            grid-template-columns: 122px 128px 92px 92px 112px 86px 92px 118px 124px 124px 98px 92px;
            align-items: center;
            min-height: 58px;
            border-bottom: 1px solid rgba(116, 147, 185, 0.12);
            transition: background 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
        }

        .ww-wallet-row:last-child {
            border-bottom: 0;
        }

        .ww-wallet-row:hover {
            background: rgba(25, 184, 255, 0.055);
            box-shadow: inset 2px 0 0 rgba(25, 184, 255, 0.48);
        }

        .ww-wallet-row.is-selected {
            background: linear-gradient(90deg, rgba(25, 184, 255, 0.12), rgba(32, 242, 156, 0.05));
            box-shadow: inset 2px 0 0 var(--ww-green), 0 0 28px rgba(25, 184, 255, 0.08);
        }

        .ww-wallet-head {
            min-height: 42px;
            background: rgba(255, 255, 255, 0.035);
            color: var(--ww-muted);
            font-size: 0.72rem;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }

        .ww-wallet-cell {
            padding: 0.65rem 0.72rem;
            color: var(--ww-text);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .ww-wallet-short {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            color: var(--ww-text);
            font-weight: 800;
        }

        .ww-num {
            display: block;
            text-align: right;
            font-variant-numeric: tabular-nums;
            font-weight: 800;
        }

        .ww-num-muted { color: var(--ww-muted); }
        .ww-num-positive { color: var(--ww-green); }
        .ww-num-negative { color: var(--ww-red); }
        .ww-num-blue { color: var(--ww-blue); }

        .ww-section {
            padding: 1rem 1.1rem;
            margin: 1rem 0;
        }

        .ww-section-head {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 1rem;
            margin: 1rem 0 0.8rem;
        }

        .ww-eyebrow {
            color: var(--ww-blue);
            font-size: 0.76rem;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .ww-section-title {
            color: var(--ww-text);
            font-size: clamp(1.35rem, 2.4vw, 2rem);
            font-weight: 850;
            margin-top: 0.15rem;
        }

        .ww-section-copy {
            color: var(--ww-muted);
            font-size: 0.94rem;
            margin-top: 0.25rem;
        }

        .ww-metric-card {
            min-height: 116px;
            padding: 1rem;
            border: 1px solid var(--ww-border);
            background: linear-gradient(145deg, rgba(10, 17, 30, 0.92), rgba(4, 8, 15, 0.9));
            box-shadow: 0 12px 44px rgba(0, 0, 0, 0.3);
            transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
            border-radius: 8px;
        }

        .ww-metric-card:hover {
            transform: translateY(-2px);
            border-color: var(--ww-border-strong);
            box-shadow: 0 18px 58px rgba(25, 184, 255, 0.09);
        }

        .ww-metric-label {
            color: var(--ww-muted);
            font-size: 0.74rem;
            text-transform: uppercase;
            font-weight: 800;
        }

        .ww-metric-value {
            margin-top: 0.5rem;
            color: var(--ww-text);
            font-size: clamp(1.45rem, 2.5vw, 2.15rem);
            font-weight: 900;
        }

        .ww-metric-note {
            margin-top: 0.45rem;
            color: var(--ww-muted);
            font-size: 0.8rem;
        }

        .ww-accent-green .ww-metric-value { color: var(--ww-green); }
        .ww-accent-blue .ww-metric-value { color: var(--ww-blue); }
        .ww-accent-red .ww-metric-value { color: var(--ww-red); }

        .ww-empty {
            border: 1px dashed rgba(142, 161, 183, 0.26);
            background: rgba(9, 15, 27, 0.66);
            padding: 1.3rem;
            color: var(--ww-muted);
            margin: 1rem 0;
            border-radius: 8px;
        }

        .ww-empty-title {
            color: var(--ww-text);
            font-weight: 800;
            font-size: 1.05rem;
            margin-bottom: 0.25rem;
        }

        .ww-live {
            width: 0.62rem;
            height: 0.62rem;
            border-radius: 999px;
            background: var(--ww-green);
            box-shadow: 0 0 0 rgba(32, 242, 156, 0.6);
            animation: wwPulse 1.8s infinite;
        }

        @keyframes wwPulse {
            0% { box-shadow: 0 0 0 0 rgba(32, 242, 156, 0.48); }
            70% { box-shadow: 0 0 0 10px rgba(32, 242, 156, 0); }
            100% { box-shadow: 0 0 0 0 rgba(32, 242, 156, 0); }
        }

        .stButton > button, .stDownloadButton > button, [data-testid="stFormSubmitButton"] button {
            border: 1px solid rgba(25, 184, 255, 0.34) !important;
            background: linear-gradient(135deg, rgba(25, 184, 255, 0.2), rgba(32, 242, 156, 0.08)) !important;
            color: var(--ww-text) !important;
            font-weight: 800 !important;
            transition: transform 150ms ease, box-shadow 150ms ease, border-color 150ms ease !important;
            border-radius: 6px !important;
        }

        .stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stFormSubmitButton"] button:hover {
            transform: translateY(-1px);
            border-color: rgba(32, 242, 156, 0.55) !important;
            box-shadow: 0 0 22px rgba(25, 184, 255, 0.18);
        }

        input, textarea, [data-baseweb="select"] > div, [data-baseweb="input"] > div {
            background-color: rgba(4, 8, 15, 0.88) !important;
            border-color: var(--ww-border) !important;
            color: var(--ww-text) !important;
            border-radius: 6px !important;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--ww-border);
            box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
            border-radius: 8px;
        }

        [data-testid="stMetric"] {
            border: 1px solid var(--ww-border);
            background: rgba(10, 17, 30, 0.84);
            padding: 0.8rem 0.9rem;
            border-radius: 8px;
        }

        [data-testid="stAlert"] {
            border: 1px solid var(--ww-border);
            background: rgba(11, 17, 29, 0.9);
            border-radius: 8px;
        }

        [data-testid="stProgress"] > div > div > div {
            background: linear-gradient(90deg, var(--ww-blue), var(--ww-green)) !important;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 0.5rem;
            border-bottom: 1px solid var(--ww-border);
        }

        .stTabs [data-baseweb="tab"] {
            background: rgba(255, 255, 255, 0.035);
            border: 1px solid var(--ww-border);
            color: var(--ww-muted);
            font-weight: 800;
            border-radius: 6px 6px 0 0;
        }

        .stTabs [aria-selected="true"] {
            border-color: var(--ww-border-strong);
            color: var(--ww-blue);
            background: var(--ww-blue-soft);
        }

        hr {
            border-color: var(--ww-border) !important;
        }

        a {
            color: var(--ww-blue) !important;
        }

        @media (max-width: 820px) {
            .block-container { padding-left: 0.9rem; padding-right: 0.9rem; }
            .ww-brand-row { align-items: flex-start; flex-direction: column; }
            .ww-topbar { position: static; }
            .ww-auth-card { padding: 1.2rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_brand_header(compact: bool = False, right_html: str = "") -> None:
    logo_class = "ww-logo ww-logo-small" if compact else "ww-logo"
    wrapper_class = "ww-topbar" if compact else "ww-auth-card"
    logo_modified_ns = LOGO_IMAGE_PATH.stat().st_mtime_ns if LOGO_IMAGE_PATH.exists() else 0
    logo_src = logo_data_uri(str(LOGO_IMAGE_PATH), logo_modified_ns)
    logo_mark = (
        f'<span class="ww-logo-mark"><img src="{logo_src}" alt="WhaleWatch logo" /></span>'
        if logo_src
        else '<span class="ww-logo-mark">WW</span>'
    )
    st.markdown(
        f"""
        <div class="{wrapper_class}">
          <div class="ww-brand-row">
            <div>
              <div class="{logo_class}">{logo_mark}{APP_NAME}</div>
              <div class="ww-tagline">{APP_TAGLINE}</div>
            </div>
            <div>{right_html}</div>
          </div>
          <div class="ww-terminal-line"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_header(title: str, eyebrow: str = "", copy: str = "", right_html: str = "") -> None:
    st.markdown(
        f"""
        <div class="ww-section-head">
          <div>
            <div class="ww-eyebrow">{eyebrow}</div>
            <div class="ww-section-title">{title}</div>
            <div class="ww-section-copy">{copy}</div>
          </div>
          <div>{right_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_compact_title(title: str, right_html: str = "") -> None:
    st.markdown(
        f"""
        <div class="ww-compact-title-row">
          <div class="ww-compact-title">{html.escape(title)}</div>
          <div>{right_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: str, note: str = "", accent: str = "blue") -> None:
    st.markdown(
        f"""
        <div class="ww-metric-card ww-accent-{accent}">
          <div class="ww-metric-label">{label}</div>
          <div class="ww-metric-value">{value}</div>
          <div class="ww-metric-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def style_financial_table(table_df: pd.DataFrame):
    styled = table_df.style
    positive_cols = [
        col
        for col in [
            "net_profit",
            "roi_pct",
            "total_volume",
            "whale_score",
            "final_score",
            "dollar_value",
            "avg_trade_size",
            "largest_trade",
            "avg_return_per_trade",
            "realized_profit_estimate",
            "unrealized_profit_estimate",
            "consistency_score",
        ]
        if col in table_df.columns
    ]
    warning_cols = [col for col in ["bot_likeness_warning"] if col in table_df.columns]

    def color_value(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if number > 0:
            return "color: #20f29c; font-weight: 800;"
        if number < 0:
            return "color: #ff526d; font-weight: 800;"
        return "color: #8ea1b7;"

    for col in positive_cols:
        styled = styled.map(color_value, subset=[col])
    if "whale_tier" in table_df.columns:
        def color_tier(value):
            tier = str(value or "").lower()
            if tier == "kraken":
                return "color: #ff526d; font-weight: 900; text-shadow: 0 0 10px rgba(255, 82, 109, 0.55);"
            if tier == "leviathan":
                return "color: #b784ff; font-weight: 900; text-shadow: 0 0 10px rgba(183, 132, 255, 0.5);"
            if tier == "blue whale":
                return "color: #19b8ff; font-weight: 900; text-shadow: 0 0 8px rgba(25, 184, 255, 0.38);"
            if tier == "shark":
                return "color: #20f29c; font-weight: 800; text-shadow: 0 0 6px rgba(32, 242, 156, 0.24);"
            return "color: #9aa8b8; font-weight: 700;"

        styled = styled.map(color_tier, subset=["whale_tier"])
    if "bot_penalty" in table_df.columns:
        def color_bot_penalty(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return ""
            if number >= 35:
                return "color: #ff526d; font-weight: 900;"
            if number >= 15:
                return "color: #ffd166; font-weight: 800;"
            return "color: #8ea1b7;"

        styled = styled.map(color_bot_penalty, subset=["bot_penalty"])
    for col in warning_cols:
        styled = styled.map(
            lambda value: "color: #ffd166; font-weight: 700;" if value else "color: #8ea1b7;",
            subset=[col],
        )
    if "wallet" in table_df.columns:
        styled = styled.map(
            lambda _value: "font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem;",
            subset=["wallet"],
        )
    if "wallet_label" in table_df.columns:
        styled = styled.map(
            lambda _value: "font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem;",
            subset=["wallet_label"],
        )
    return styled


def render_empty_state(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="ww-empty">
          <div class="ww-empty-title">{title}</div>
          <div>{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def badge(text: str, tone: str = "blue") -> str:
    return f'<span class="ww-badge ww-badge-{tone}">{text}</span>'


def tier_class(tier: str) -> str:
    return str(tier or "Dolphin").lower().replace(" ", "-")


def tier_html(tier: str) -> str:
    safe_tier = html.escape(str(tier or "Dolphin"))
    return f'<span class="ww-tier ww-tier-{tier_class(safe_tier)}">{safe_tier}</span>'


TIER_NAMES = {"Kraken", "Leviathan", "Blue Whale", "Shark", "Dolphin"}


def normalize_tier_name(value) -> str:
    text = str(value or "").strip()
    for tier in TIER_NAMES:
        if text.lower() == tier.lower():
            return tier
    return "Dolphin"


def watchlist_item_map() -> dict[str, dict]:
    return {
        str(item.get("wallet", "")).lower(): item
        for item in st.session_state.get("watchlist_items", [])
        if item.get("wallet")
    }


def wallet_tier(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet_key = str(wallet or "").lower()
    ranked_lookup = ranked_lookup or {}
    ranked_row = ranked_lookup.get(wallet_key, {})
    item = watchlist_item_map().get(wallet_key, {})
    return normalize_tier_name(
        ranked_row.get("whale_tier")
        or item.get("whale_tier")
        or item.get("wallet_label")
    )


def wallet_label_text(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet = str(wallet or "")
    return f"[{wallet_tier(wallet, ranked_lookup)}] {wallet}"


def wallet_badge_html(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet = str(wallet or "")
    tier = wallet_tier(wallet, ranked_lookup)
    return (
        f'<span class="ww-wallet-inline">{tier_html(tier)}'
        f'<span class="ww-wallet-address compact">{html.escape(wallet)}</span></span>'
    )


def short_wallet(wallet: str) -> str:
    wallet = str(wallet or "")
    return f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet


def signal_badges(row: dict) -> str:
    signals = []
    if (row.get("adjusted_win_rate") or row.get("win_rate") or 0) >= 60:
        signals.append("High Win Rate")
    if (row.get("roi_pct") or 0) >= 20:
        signals.append("Strong ROI")
    if (row.get("total_volume") or 0) >= 50000:
        signals.append("High Volume")
    if (row.get("avg_trade_size") or 0) >= 500:
        signals.append("Large Positions")
    if (row.get("trend_score") or 0) >= 50 or (row.get("recent_activity") or 0) >= 20:
        signals.append("Trending")
    if (row.get("consistency_score") or 0) >= 60:
        signals.append("Consistent")
    return ", ".join(signals[:3]) or "Balanced"


def safe_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def number_tone(value, blue_zero: bool = False) -> str:
    number = safe_number(value)
    if number > 0:
        return "ww-num-positive"
    if number < 0:
        return "ww-num-negative"
    return "ww-num-blue" if blue_zero else "ww-num-muted"


def format_money(value) -> str:
    number = safe_number(value)
    if number < 0:
        return f"-${abs(number):,.2f}"
    return f"${number:,.2f}"


def format_percent(value) -> str:
    return f"{safe_number(value):.2f}%"


def format_optional_percent(value, fallback: str = "Insufficient data") -> str:
    number = optional_metric_number(value)
    return f"{number:.2f}%" if number is not None else fallback


def format_optional_money(value, fallback: str = "Insufficient data") -> str:
    number = optional_metric_number(value)
    return format_money(number) if number is not None else fallback


def format_optional_score(value, fallback: str = "Calculating...") -> str:
    number = optional_metric_number(value)
    return f"{number:.2f}" if number is not None else fallback


def format_score(value) -> str:
    return f"{safe_number(value):.2f}"


inject_whalewatch_theme()


def api_client() -> PolymarketClient:
    return PolymarketClient()


@st.cache_resource
def db_connection():
    return connect()


def read_firebase_secrets() -> tuple[str, str] | None:
    try:
        api_key = str(st.secrets["FIREBASE_WEB_API_KEY"]).strip()
        project_id = str(st.secrets["FIREBASE_PROJECT_ID"]).strip()
    except Exception:
        return None
    if not api_key or not project_id:
        return None
    return api_key, project_id


@st.cache_resource
def firebase_store(api_key: str, project_id: str) -> FirebaseStore:
    return FirebaseStore(api_key, project_id)


def get_firebase_store() -> FirebaseStore | None:
    config = read_firebase_secrets()
    if not config:
        return None
    return firebase_store(*config)


def show_firebase_error(action: str, exc: FirebaseError) -> None:
    if exc.status_code is None and str(exc) == FIREBASE_CONNECTION_ERROR:
        st.error("Login service is temporarily unavailable. Please try again later.")
    else:
        st.error(f"{action} failed: {friendly_auth_error(str(exc))}")


def friendly_auth_error(message: str) -> str:
    normalized = message.upper()
    if "EMAIL_EXISTS" in normalized:
        return "An account with that email already exists."
    if "EMAIL_NOT_FOUND" in normalized or "INVALID_PASSWORD" in normalized or "INVALID_LOGIN_CREDENTIALS" in normalized:
        return "The email or password is incorrect."
    if "WEAK_PASSWORD" in normalized:
        return "Please choose a stronger password."
    if "TOO_MANY_ATTEMPTS" in normalized:
        return "Too many attempts. Please wait a moment and try again."
    return "Please check your details and try again."


def reset_user_runtime_state() -> None:
    for key in [
        "watchlist_items",
        "watchlist",
        "crypto_watchlist_items",
        "crypto_watchlist",
        "seen_trade_ids",
        "seen_trade_timestamps",
        "watchlist_alert_wallets_initialized",
        "new_trade_alerts",
        "crypto_seen_trade_ids",
        "crypto_seen_trade_timestamps",
        "crypto_alert_wallets_initialized",
        "crypto_new_trade_alerts",
        "user_settings",
        "rows",
        "crypto_rows",
        "discovered_wallets",
        "crypto_discovered_wallets",
        "recent_trades",
        "crypto_recent_activity",
        "selected_wallet",
        "selected_crypto_wallet",
        "show_selected_wallet_trades",
        "show_selected_crypto_trades",
    ]:
        st.session_state.pop(key, None)


def save_auth_session(session) -> None:
    st.session_state["firebase_session"] = session.to_dict()


def current_auth_session() -> dict | None:
    session = st.session_state.get("firebase_session")
    if not session or not session.get("id_token") or not session.get("user_id"):
        return None
    if int(session.get("expires_at") or 0) <= int(time.time()) + 60:
        refresh_token = session.get("refresh_token")
        store = get_firebase_store()
        if not refresh_token or not store:
            st.session_state.pop("firebase_session", None)
            return None
        try:
            refreshed = store.refresh_session(str(refresh_token))
        except FirebaseError:
            logger.warning("Could not refresh Firebase session", exc_info=True)
            st.session_state.pop("firebase_session", None)
            return None
        refreshed_session = refreshed.to_dict()
        if not refreshed_session.get("email"):
            refreshed_session["email"] = session.get("email", "")
        st.session_state["firebase_session"] = refreshed_session
        session = st.session_state["firebase_session"]
    return session


def render_auth_page() -> None:
    render_brand_header()
    store = get_firebase_store()
    if not store:
        st.error("Login is temporarily unavailable. Please contact the app owner.")
        st.stop()

    render_section_header(
        "Secure Access",
        "Authentication",
        "Sign in or create an account to unlock private watchlists, alerts, and saved settings.",
        right_html=badge("Read-only", "green"),
    )
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
            except FirebaseError as exc:
                show_firebase_error("Login", exc)
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
            except FirebaseError as exc:
                show_firebase_error("Signup", exc)
            else:
                reset_user_runtime_state()
                save_auth_session(session)
                st.rerun()


def sign_out() -> None:
    store = get_firebase_store()
    if store:
        store.logout()
    st.session_state.pop("firebase_session", None)
    st.session_state.pop("loaded_user_id", None)
    reset_user_runtime_state()
    st.rerun()


def sync_user_data_from_firebase(force: bool = False) -> None:
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        return
    user_id = str(session["user_id"])
    if not force and st.session_state.get("loaded_user_id") == user_id:
        ensure_watchlist_state()
        ensure_crypto_watchlist_state()
        return
    try:
        st.session_state["watchlist_items"] = store.fetch_watchlist(
            str(session["id_token"]),
            user_id,
            collection="polymarket_watchlist",
        )
        if not st.session_state["watchlist_items"]:
            st.session_state["watchlist_items"] = store.fetch_watchlist(
                str(session["id_token"]),
                user_id,
                collection="watchlist",
            )
        st.session_state["crypto_watchlist_items"] = store.fetch_watchlist(
            str(session["id_token"]),
            user_id,
            collection="crypto_watchlist",
        )
        st.session_state["user_settings"] = store.fetch_user_settings(str(session["id_token"]), user_id)
        st.session_state["loaded_user_id"] = user_id
    except FirebaseError:
        logger.warning("Could not load account data", exc_info=True)
        st.error("Could not load your account data. Please try again later.")
        st.stop()
    ensure_watchlist_state()
    ensure_crypto_watchlist_state()


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


def ensure_crypto_watchlist_state() -> None:
    if "crypto_watchlist_items" not in st.session_state:
        st.session_state["crypto_watchlist_items"] = []
    st.session_state["crypto_watchlist"] = [
        str(item.get("wallet", "")).lower()
        for item in st.session_state["crypto_watchlist_items"]
        if item.get("wallet")
    ]
    st.session_state.setdefault("crypto_seen_trade_ids", [])
    st.session_state.setdefault("crypto_seen_trade_timestamps", {})
    st.session_state.setdefault("crypto_alert_wallets_initialized", [])
    st.session_state.setdefault("crypto_new_trade_alerts", [])


def add_wallet_to_watchlist(wallet: str, row: dict | None = None) -> None:
    wallet = wallet.lower()
    ensure_watchlist_state()
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        st.error("Log in before adding wallets to your watchlist.")
        return
    try:
        saved_items = store.upsert_watchlist(
            str(session["id_token"]),
            str(session["user_id"]),
            wallet,
            wallet_label=normalize_tier_name(row.get("whale_tier")) if row else None,
            collection="polymarket_watchlist",
        )
    except FirebaseError:
        logger.warning("Could not add wallet to watchlist", exc_info=True)
        st.error("Could not add wallet to watchlist right now.")
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
                "whale_tier": normalize_tier_name(row.get("whale_tier")),
            }
        )
    existing[wallet] = item
    st.session_state["watchlist_items"] = list(existing.values())
    ensure_watchlist_state()


def remove_wallet_from_watchlist(wallet: str) -> None:
    wallet = wallet.lower()
    ensure_watchlist_state()
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        st.error("Log in before changing your watchlist.")
        return
    try:
        store.delete_watchlist_wallet_from_collection(
            str(session["id_token"]),
            str(session["user_id"]),
            wallet,
            collection="polymarket_watchlist",
        )
    except FirebaseError:
        logger.warning("Could not remove wallet from watchlist", exc_info=True)
        st.error("Could not remove wallet from watchlist right now.")
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


def firestore_scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)
    text = str(value).strip()
    return text if text else None


def optional_metric_number(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def crypto_completed_cycles(row: dict | pd.Series | None) -> int:
    if row is None:
        return 0
    return int(safe_number(row.get("completed_trades") if hasattr(row, "get") else 0))


def crypto_profitable_trade_pct_value(row: dict | pd.Series | None):
    if crypto_completed_cycles(row) <= 0 or row is None:
        return None
    value = row.get("profitable_trade_pct") if row.get("profitable_trade_pct") is not None else row.get("win_rate")
    return optional_metric_number(value)


def crypto_estimated_roi_value(row: dict | pd.Series | None):
    if crypto_completed_cycles(row) <= 0 or row is None:
        return None
    return optional_metric_number(row.get("roi_pct") if row.get("roi_pct") is not None else row.get("roi"))


def crypto_estimated_profit_value(row: dict | pd.Series | None):
    if crypto_completed_cycles(row) <= 0 or row is None:
        return None
    return optional_metric_number(row.get("net_profit"))


def crypto_watchlist_metric_payload(row: dict | None, last_activity: str | None = None) -> dict:
    if not row:
        return {}
    roi = crypto_estimated_roi_value(row)
    average_trade_size = optional_metric_number(
        row.get("avg_trade_size") if row.get("avg_trade_size") is not None else row.get("average_trade_size")
    )
    tier = normalize_tier_name(row.get("whale_tier") or row.get("tier") or row.get("wallet_label"))
    payload = {
        "wallet_label": tier,
        "whale_tier": tier,
        "tier": tier,
        "whale_score": optional_metric_number(row.get("whale_score")),
        "net_profit": crypto_estimated_profit_value(row),
        "roi_pct": roi,
        "roi": roi,
        "win_rate": crypto_profitable_trade_pct_value(row),
        "profitable_trade_pct": crypto_profitable_trade_pct_value(row),
        "profitable_swap_pct": crypto_profitable_trade_pct_value(row),
        "total_volume": optional_metric_number(row.get("total_volume")),
        "avg_trade_size": average_trade_size,
        "average_trade_size": average_trade_size,
        "last_activity": firestore_scalar(last_activity or row.get("last_activity")),
        "chain": firestore_scalar(row.get("chain")),
        "completed_trades": optional_metric_number(row.get("completed_trades")),
        "avg_profit_per_completed_trade": optional_metric_number(row.get("avg_profit_per_completed_trade")),
        "recent_profitable_trades": optional_metric_number(row.get("recent_profitable_trades")),
        "trading_frequency": optional_metric_number(row.get("trading_frequency")),
        "confidence": firestore_scalar(row.get("confidence")),
        "confidence_note": firestore_scalar(row.get("confidence_note")),
        "copy_quality": firestore_scalar(row.get("copy_quality")),
    }
    return {key: value for key, value in payload.items() if value is not None}


def crypto_watchlist_has_core_metrics(item: dict) -> bool:
    return all(
        optional_metric_number(item.get(key)) is not None
        for key in ["whale_score", "total_volume", "avg_trade_size", "completed_trades"]
    )


def add_crypto_wallet_to_watchlist(wallet: str, row: dict | None = None) -> None:
    wallet = str(wallet).lower()
    ensure_crypto_watchlist_state()
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        st.error("Log in before adding wallets to your crypto watchlist.")
        return
    metadata = crypto_watchlist_metric_payload(row, row.get("last_activity") if row else None)
    try:
        saved_items = store.upsert_watchlist(
            str(session["id_token"]),
            str(session["user_id"]),
            wallet,
            wallet_label=normalize_tier_name(row.get("whale_tier")) if row else None,
            collection="crypto_watchlist",
            metadata=metadata,
        )
    except FirebaseError:
        logger.warning("Could not add crypto wallet to watchlist", exc_info=True)
        st.error("Could not add crypto wallet to watchlist right now.")
        return
    existing = {item["wallet"].lower(): item for item in st.session_state["crypto_watchlist_items"]}
    item = saved_items[0] if saved_items else existing.get(wallet, {"wallet": wallet})
    if row:
        item.update({"wallet": wallet, **metadata})
    existing[wallet] = item
    st.session_state["crypto_watchlist_items"] = list(existing.values())
    ensure_crypto_watchlist_state()


def persist_crypto_watchlist_metrics(wallet: str, row: dict, last_activity: str | None = None) -> dict:
    wallet = str(wallet).lower()
    metadata = crypto_watchlist_metric_payload(row, last_activity)
    if not metadata:
        return {}
    session = current_auth_session()
    store = get_firebase_store()
    if session and store:
        try:
            store.upsert_watchlist(
                str(session["id_token"]),
                str(session["user_id"]),
                wallet,
                wallet_label=normalize_tier_name(metadata.get("whale_tier")),
                collection="crypto_watchlist",
                metadata=metadata,
            )
        except FirebaseError:
            logger.warning("Could not persist crypto watchlist metrics for %s", wallet, exc_info=True)
    existing = {str(item.get("wallet", "")).lower(): item for item in st.session_state.get("crypto_watchlist_items", [])}
    item = existing.get(wallet, {"wallet": wallet})
    item.update({"wallet": wallet, **metadata})
    existing[wallet] = item
    st.session_state["crypto_watchlist_items"] = list(existing.values())
    ensure_crypto_watchlist_state()
    return metadata


def remove_crypto_wallet_from_watchlist(wallet: str) -> None:
    wallet = str(wallet).lower()
    ensure_crypto_watchlist_state()
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        st.error("Log in before changing your crypto watchlist.")
        return
    try:
        store.delete_watchlist_wallet_from_collection(
            str(session["id_token"]),
            str(session["user_id"]),
            wallet,
            collection="crypto_watchlist",
        )
    except FirebaseError:
        logger.warning("Could not remove crypto wallet from watchlist", exc_info=True)
        st.error("Could not remove crypto wallet from watchlist right now.")
        return
    st.session_state["crypto_watchlist_items"] = [
        item for item in st.session_state["crypto_watchlist_items"] if str(item.get("wallet", "")).lower() != wallet
    ]
    st.session_state["crypto_alert_wallets_initialized"] = [
        item
        for item in st.session_state.get("crypto_alert_wallets_initialized", [])
        if str(item).lower() != wallet
    ]
    ensure_crypto_watchlist_state()


def user_settings() -> dict:
    return {**DEFAULT_USER_SETTINGS, **st.session_state.get("user_settings", {})}


def update_user_settings(**updates) -> None:
    current = user_settings()
    changed = any(current.get(key) != value for key, value in updates.items())
    if not changed:
        return
    session = current_auth_session()
    store = get_firebase_store()
    if not session or not store:
        return
    updated = {**current, **updates}
    try:
        st.session_state["user_settings"] = store.upsert_user_settings(
            str(session["id_token"]),
            str(session["user_id"]),
            updated,
        )
    except FirebaseError:
        logger.warning("Could not save account settings", exc_info=True)
        st.warning("Could not save settings right now.")


@dataclass
class RunBudget:
    started_at: float
    api_calls: int = 0
    max_api_calls: int = DEFAULT_MAX_API_CALLS_PER_RUN
    max_seconds: int = DEFAULT_MAX_RUN_SECONDS

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


def crypto_api_key(chain: str) -> str | None:
    key_names = {
        "Ethereum": ["ETHERSCAN_API_KEY"],
        "BNB Chain": ["BSCSCAN_API_KEY"],
        "Base": ["ETHERSCAN_API_KEY", "BASESCAN_API_KEY"],
        "Arbitrum": ["ETHERSCAN_API_KEY", "ARBISCAN_API_KEY"],
    }
    for name in key_names.get(chain, []):
        try:
            value = str(st.secrets.get(name, "")).strip()
        except Exception:
            value = ""
        if value:
            return value
    return None


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


@st.cache_data(ttl=10 * 60, show_spinner=False)
def cached_discover_crypto_whales(
    chain: str,
    api_key: str | None,
    token_filter: str,
    min_transaction_size: float,
    max_wallets: int,
    time_period_days: int,
    include_cex_related: bool,
    seed_wallets: tuple[str, ...],
) -> list[str]:
    return discover_crypto_whales(
        chain=chain,
        api_key=api_key,
        token_filter=token_filter,
        min_transaction_size=float(min_transaction_size),
        max_wallets=int(max_wallets),
        time_period_days=int(time_period_days),
        include_cex_related=bool(include_cex_related),
        seed_wallets=list(seed_wallets),
    )


@st.cache_data(ttl=5 * 60, show_spinner=False)
def cached_crypto_wallet_activity(
    wallet: str,
    chain: str,
    api_key: str | None,
    token_filter: str,
    time_period_days: int,
) -> list[dict]:
    return fetch_crypto_wallet_activity(wallet, chain, api_key, token_filter, int(time_period_days))


@st.cache_data(ttl=5 * 60, show_spinner=False)
def cached_crypto_recent_trades(
    wallet: str,
    chain: str,
    api_key: str | None,
    token_filter: str,
    time_period_days: int,
) -> list[dict]:
    return fetch_crypto_recent_trades(wallet, chain, api_key, token_filter, int(time_period_days))


@st.cache_data(ttl=10 * 60, show_spinner=False)
def cached_crypto_token_balances(wallet: str, chain: str, api_key: str | None) -> list[dict]:
    return fetch_crypto_token_balances(wallet, chain, api_key)


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
    max_runtime_seconds: int,
    progress=None,
    status=None,
    started_at: float | None = None,
) -> tuple[list[str], list[dict]]:
    day_seconds = 24 * 3600
    page_size = min(500, max_recent_trades)
    max_scan_rows = min(max_recent_trades, MAX_OFFSET)
    now_ts = int(time.time())
    since_ts = int(time.time()) - time_period_days * 24 * 3600
    wallet_volume: dict[str, float] = {}
    selected_trades: list[dict] = []
    scanned = 0
    started_at = started_at or time.time()

    for day_index in range(time_period_days):
        if time.time() - started_at > max_runtime_seconds:
            break
        window_end = now_ts - day_index * day_seconds
        window_start = max(since_ts, window_end - day_seconds)
        if window_end <= since_ts:
            break
        for offset in range(0, max_scan_rows, page_size):
            if time.time() - started_at > max_runtime_seconds:
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
        unique_markets = resolved_markets
        neutral_win_rate = 50.0
        volume_score = normalize_to_100(total_volume, 300000)
        sizing_score = min(
            100.0,
            normalize_to_100(avg_trade_size, 2500) * 0.65 + normalize_to_100(largest_trade, 10000) * 0.35,
        )
        consistency_score = max(0.0, min(100.0, (1 - same_size_ratio) * 100))
        activity_frequency_score = min(
            100.0,
            normalize_to_100(trade_count, 120) * 0.60 + normalize_to_100(unique_markets, 30) * 0.40,
        )
        bot_penalty = bot_likeness_penalty(
            trade_count,
            avg_trade_size,
            largest_trade,
            same_size_ratio,
            total_volume,
            unique_markets,
        )
        final_score = min(
            100.0,
            max(
                0.0,
                neutral_win_rate * 0.35
                + volume_score * 0.30
                + sizing_score * 0.20
                + consistency_score * 0.10
                + activity_frequency_score * 0.05
                - bot_penalty,
            ),
        )
        warning = bot_likeness_warning(
            trade_count,
            avg_trade_size,
            largest_trade,
            same_size_ratio,
            0.0,
            total_volume,
        )
        whale_score = min(
            100.0,
            max(
                0.0,
                neutral_win_rate * 0.25
                + volume_score * 0.20
                + sizing_score * 0.10
                + consistency_score * 0.10
                + activity_frequency_score * 0.05
                - bot_penalty,
            ),
        )
        tier = whale_tier(whale_score)
        why = []
        if total_volume >= 25000:
            why.append(f"${total_volume:,.0f} recent traded volume")
        if avg_trade_size >= 250:
            why.append(f"${avg_trade_size:,.0f} average position")
        if largest_trade >= 1000:
            why.append(f"${largest_trade:,.0f} largest recent trade")
        if unique_markets >= 5:
            why.append(f"{unique_markets} active markets")
        if bot_penalty >= 20:
            why.append("penalized for bot-like repetition")
        rows.append(
            {
                "wallet": wallet,
                "polygonscan_url": f"https://polygonscan.com/address/{wallet}",
                "polymarket_profile_url": f"https://polymarket.com/profile/{wallet}",
                "adjusted_win_rate": neutral_win_rate,
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
                "consistency_score": round(consistency_score, 2),
                "activity_frequency_score": round(activity_frequency_score, 2),
                "position_sizing_score": round(sizing_score, 2),
                "bot_penalty": round(bot_penalty, 2),
                "final_score": round(final_score, 2),
                "whale_score": round(whale_score, 2),
                "whale_tier": tier,
                "bot_likeness_warning": warning,
                "why_ranked_highly": "; ".join(why[:4]) or "ranked by recent volume, sizing, and activity balance",
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


def strong_profit_volume(row: dict, min_profit: float, min_volume: float) -> bool:
    return (row.get("net_profit") or 0) >= max(min_profit, 0) and (row.get("total_volume") or 0) >= max(min_volume * 1.5, 25000)


def whale_row_quality_passes(
    row: dict,
    min_whale_score: float,
    min_profit: float,
    min_volume: float,
    min_win_rate: float,
    min_roi: float,
    min_avg_trade_size: float,
    min_largest_trade: float,
    min_resolved: int,
    include_aggressive_traders: bool,
    include_bots: bool,
    fast_mode_results: bool,
) -> bool:
    whale_score = float(row.get("whale_score") or 0)
    total_volume = float(row.get("total_volume") or 0)
    avg_trade_size = float(row.get("avg_trade_size") or 0)
    largest_trade = float(row.get("largest_trade") or 0)
    net_profit = float(row.get("net_profit") or 0)
    roi_pct = row.get("roi_pct")
    win_rate = row.get("win_rate")
    bot_penalty = float(row.get("bot_penalty") or 0)
    sample_count = max(int(row.get("resolved_markets") or 0), int(row.get("trade_count") or 0))
    strong_capital = total_volume >= max(min_volume * 2, 30000) and avg_trade_size >= max(min_avg_trade_size, 150)
    aggressive = include_aggressive_traders and strong_capital and largest_trade >= max(min_largest_trade, 1000)

    if total_volume <= 0 and sample_count <= 0:
        return False
    if not include_bots and bot_penalty >= 45:
        return False
    if whale_score < min_whale_score and not strong_capital:
        return False
    if sample_count < min_resolved and not strong_capital:
        return False
    if total_volume < min_volume and whale_score < min_whale_score + 15:
        return False
    if avg_trade_size < min_avg_trade_size and not strong_capital:
        return False
    if largest_trade < min_largest_trade and not aggressive:
        return False
    if not fast_mode_results:
        if net_profit < min_profit and not aggressive:
            return False
        if win_rate is not None and float(win_rate) < min_win_rate and not strong_profit_volume(row, min_profit, min_volume):
            return False
        if roi_pct is not None and float(roi_pct) < min_roi and not strong_profit_volume(row, min_profit, min_volume):
            return False
    return True


def rank_whale_rows(
    rows: list[dict],
    min_whale_score: float,
    min_profit: float,
    min_volume: float,
    min_win_rate: float,
    min_roi: float,
    min_avg_trade_size: float,
    min_largest_trade: float,
    min_resolved: int,
    include_aggressive_traders: bool,
    include_bots: bool,
    fast_mode_results: bool,
    max_rows: int,
) -> tuple[list[dict], str]:
    usable = [
        row for row in rows
        if (row.get("total_volume") or 0) > 0 or (row.get("resolved_markets") or 0) > 0 or (row.get("trade_count") or 0) > 0
    ]
    ranked = sorted(usable, key=lambda item: item.get("whale_score", 0), reverse=True)
    preferred = [
        row for row in ranked
        if whale_row_quality_passes(
            row,
            min_whale_score,
            min_profit,
            min_volume,
            min_win_rate,
            min_roi,
            min_avg_trade_size,
            min_largest_trade,
            min_resolved,
            include_aggressive_traders,
            include_bots,
            fast_mode_results,
        )
    ]
    if preferred:
        return preferred, "preferred thresholds"
    fallback = ranked[: max(1, min(max_rows, len(ranked)))]
    reason = "best available wallets shown because no wallet passed every preferred threshold"
    if not usable:
        reason = "no usable data exists"
    return fallback, reason


def trending_whales(rows: list[dict], limit: int = 6) -> pd.DataFrame:
    usable = [row for row in rows if (row.get("recent_activity") or 0) > 0]
    if not usable:
        return pd.DataFrame()
    activities = sorted(float(row.get("recent_activity") or 0) for row in usable)
    median_activity = activities[len(activities) // 2]
    trending = []
    for row in usable:
        recent_activity = float(row.get("recent_activity") or 0)
        trend_score = (
            normalize_to_100(recent_activity, max(median_activity * 3, 30)) * 0.45
            + normalize_to_100(float(row.get("total_volume") or 0), 150000) * 0.30
            + normalize_to_100(float(row.get("avg_trade_size") or 0), 2000) * 0.15
            + float(row.get("whale_score") or 0) * 0.10
        )
        if recent_activity >= max(5, median_activity * 1.5) or trend_score >= 35:
            item = dict(row)
            item["trend_score"] = round(min(100, trend_score), 2)
            trending.append(item)
    return pd.DataFrame(sorted(trending, key=lambda row: row.get("trend_score", 0), reverse=True)[:limit])


def add_compact_wallet_table_fields(df: pd.DataFrame) -> pd.DataFrame:
    display_df = df.copy()
    if "wallet" not in display_df.columns:
        display_df["wallet"] = ""
    if "trend_score" not in display_df.columns:
        trend_lookup = {}
        trend_df = trending_whales(display_df.to_dict("records"), limit=len(display_df))
        if not trend_df.empty and "wallet" in trend_df.columns:
            trend_lookup = {
                str(row["wallet"]).lower(): row.get("trend_score", 0)
                for _, row in trend_df.iterrows()
            }
        display_df["trend_score"] = display_df["wallet"].astype(str).str.lower().map(trend_lookup).fillna(0.0)
    display_df["wallet_full"] = display_df["wallet"].astype(str)
    display_df["wallet_display"] = display_df["wallet_full"].map(short_wallet)
    display_df["signals"] = [signal_badges(row) for row in display_df.to_dict("records")]
    return display_df


def render_selected_wallet_panel(selected_wallet: str, selected_metadata: dict | None) -> None:
    if not selected_metadata:
        return
    tier = str(selected_metadata.get("whale_tier") or "Dolphin")
    signals = [item.strip() for item in signal_badges(selected_metadata).split(",") if item.strip()]
    explanation = str(selected_metadata.get("why_ranked_highly") or "Balanced profit, volume, activity, and copyability signals.")
    st.markdown(
        f"""
        <div class="ww-wallet-panel">
          <div class="ww-brand-row">
            <div>
              <div class="ww-eyebrow">Selected Wallet</div>
              <div style="margin: 0.35rem 0;">{wallet_badge_html(selected_wallet, {selected_wallet.lower(): selected_metadata})}</div>
            </div>
            <div class="ww-pill-row">{''.join(badge(item, "green" if item in ("Strong ROI", "High Win Rate", "Consistent") else "blue") for item in signals[:3])}</div>
          </div>
          <div class="ww-terminal-line"></div>
          <div class="ww-section-copy">{html.escape(explanation)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    metric_cols = st.columns(5)
    with metric_cols[0]:
        render_metric_card("Win Rate", f"{float(selected_metadata.get('win_rate') or 0):.2f}%", "Resolved markets", "blue")
    with metric_cols[1]:
        render_metric_card("ROI", f"{float(selected_metadata.get('roi_pct') or 0):.2f}%", "Return profile", "green" if (selected_metadata.get("roi_pct") or 0) >= 0 else "red")
    with metric_cols[2]:
        render_metric_card("Net Profit", f"${float(selected_metadata.get('net_profit') or 0):,.2f}", "Realized P/L", "green" if (selected_metadata.get("net_profit") or 0) >= 0 else "red")
    with metric_cols[3]:
        render_metric_card("Total Volume", f"${float(selected_metadata.get('total_volume') or 0):,.2f}", "Capital flow", "blue")
    with metric_cols[4]:
        render_metric_card("Whale Score", f"{float(selected_metadata.get('whale_score') or 0):.2f}", "Weighted rank", "green")
    st.caption("Copy wallet address")
    st.code(selected_wallet, language="text")
    action_cols = st.columns([1, 1, 3])
    if action_cols[0].button(
        "Add to Watchlist",
        disabled=selected_wallet in st.session_state["watchlist"],
        key="add-selected-wallet",
    ):
        add_wallet_to_watchlist(selected_wallet, selected_metadata)
        st.rerun()
    if action_cols[1].button("View Recent Trades", key="selected-wallet-recent-trades"):
        st.session_state["selected_wallet"] = selected_wallet
        st.session_state["trade_viewer_wallet"] = selected_wallet
        st.session_state["show_selected_wallet_trades"] = selected_wallet
        st.rerun()


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


def render_wallet_trade_viewer(
    wallet_options: list[str],
    selected_wallet: str | None = None,
    ranked_lookup: dict[str, dict] | None = None,
    require_watchlist: bool = False,
) -> None:
    ranked_lookup = ranked_lookup or {}
    wallet_options = list(dict.fromkeys(str(wallet).lower() for wallet in wallet_options if wallet))
    selected_wallet = str(selected_wallet or "").lower()

    render_compact_title("Wallet Details", badge("Read-only", "green"))

    if require_watchlist and not wallet_options:
        render_empty_state("Wallet Details", "Add wallets to your watchlist before viewing wallet details.")
        return

    if wallet_options:
        selected_index = wallet_options.index(selected_wallet) if selected_wallet in wallet_options else 0
        viewer_wallet = st.selectbox(
            "Select wallet",
            wallet_options,
            index=selected_index,
            format_func=lambda wallet: wallet_label_text(wallet, ranked_lookup),
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
        render_empty_state("No wallet selected", "Add wallets to your watchlist before viewing wallet details.")
        return

    st.markdown(wallet_badge_html(viewer_wallet, ranked_lookup), unsafe_allow_html=True)

    add_disabled = viewer_wallet in st.session_state["watchlist"]
    if not require_watchlist and viewer_cols[4].button("Add to Watchlist", disabled=add_disabled):
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
    if not trade_df.empty:
        trade_df["wallet_label"] = wallet_label_text(viewer_wallet, ranked_lookup)

    trade_columns = [
        "wallet_label",
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
        render_empty_state("No matching trades", "Adjust the time window, side, open-market, or minimum size filters.")
        return
    st.dataframe(
        style_financial_table(trade_df[trade_columns]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "wallet_label": st.column_config.TextColumn("Wallet", width=430),
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


def collect_watchlist_trades(ranked_lookup: dict[str, dict], whale_mode: bool) -> pd.DataFrame:
    watchlist_frames = []
    whale_wallets = set(ranked_lookup.keys()) if whale_mode else set()
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
        wallet_trades["wallet_tier"] = wallet_tier(wallet, ranked_lookup)
        wallet_trades["wallet_label"] = wallet_label_text(wallet, ranked_lookup)
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
        f"New whale trade: {alert.get('wallet_label') or alert.get('wallet')} {verb} {alert.get('outcome') or 'UNKNOWN'} "
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
                "wallet_tier": row.get("wallet_tier") or wallet_tier(wallet),
                "wallet_label": row.get("wallet_label") or wallet_label_text(wallet),
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
    alerts = st.session_state.get("new_trade_alerts", [])
    render_compact_title("New Alerts", badge(f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}", "green"))
    if not alerts:
        render_empty_state("No new alerts", "Trades above your alert minimum will appear here.")
        return
    clear_col, count_col = st.columns([1, 4])
    if clear_col.button("Clear alerts", key="clear-watchlist-alerts"):
        st.session_state["new_trade_alerts"] = []
        st.rerun()
    count_col.caption("Newest first")
    for index, alert in enumerate(alerts[:12], start=1):
        wallet = str(alert.get("wallet") or "")
        tier = normalize_tier_name(alert.get("wallet_tier")) if alert.get("wallet_tier") else wallet_tier(wallet)
        wallet_html = (
            f'<span class="ww-wallet-inline">{tier_html(tier)}'
            f'<span class="ww-wallet-address compact">{html.escape(wallet)}</span></span>'
        )
        market = html.escape(str(alert.get("market") or "Unknown market"))
        market_link = str(alert.get("market_link") or "")
        market_html = f'<a href="{html.escape(market_link)}" target="_blank">{market}</a>' if market_link else market
        side = html.escape(str(alert.get("side") or "").title())
        outcome = html.escape(str(alert.get("outcome") or ""))
        price = safe_number(alert.get("price"))
        size = safe_number(alert.get("size"))
        value = safe_number(alert.get("dollar_value"))
        timestamp = html.escape(str(alert.get("timestamp") or ""))
        st.markdown(
            f"""
            <div class="ww-alert-row">
              <div class="ww-brand-row">
                <div>{wallet_html}</div>
                <div class="ww-num ww-num-positive">{format_money(value)}</div>
              </div>
              <div class="ww-alert-market">{market_html}</div>
              <div class="ww-section-copy">{side} {outcome} at {price:.4f} · {size:,.2f} shares · {timestamp}</div>
            </div>
            """,
            unsafe_allow_html=True,
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
    watchlist_df = collect_watchlist_trades(ranked_lookup, whale_mode)
    detect_watchlist_alerts(watchlist_df, alert_min_trade_value, enable_popup_alerts, play_sound)
    render_new_alerts()

    render_compact_title("Saved Wallets")
    header = st.columns([1.25, 5, 1.25, 1.35, 1.05, 2, 1])
    header[0].markdown('<div class="ww-watchlist-head">Tier</div>', unsafe_allow_html=True)
    header[1].markdown('<div class="ww-watchlist-head">Wallet address</div>', unsafe_allow_html=True)
    header[2].markdown('<div class="ww-watchlist-head">Whale score</div>', unsafe_allow_html=True)
    header[3].markdown('<div class="ww-watchlist-head">Net profit</div>', unsafe_allow_html=True)
    header[4].markdown('<div class="ww-watchlist-head">ROI</div>', unsafe_allow_html=True)
    header[5].markdown('<div class="ww-watchlist-head">Last trade</div>', unsafe_allow_html=True)
    header[6].markdown('<div class="ww-watchlist-head">Remove</div>', unsafe_allow_html=True)

    last_trade_times = last_trade_times_from_df(watchlist_df)
    for item in list(st.session_state["watchlist_items"]):
        wallet = str(item.get("wallet", "")).lower()
        merged = {**item, **ranked_lookup.get(wallet, {})}
        last_trade = last_trade_times.get(wallet, "")
        tier = wallet_tier(wallet, ranked_lookup)
        cols = st.columns([1.25, 5, 1.25, 1.35, 1.05, 2, 1])
        cols[0].markdown(tier_html(tier), unsafe_allow_html=True)
        cols[1].markdown(f'<span class="ww-wallet-address compact">{html.escape(wallet)}</span>', unsafe_allow_html=True)
        cols[2].write("" if merged.get("whale_score") is None else f"{safe_number(merged.get('whale_score')):.2f}")
        cols[3].write("" if merged.get("net_profit") is None else format_money(merged.get("net_profit")))
        cols[4].write("" if merged.get("roi_pct") is None else format_percent(merged.get("roi_pct")))
        cols[5].write(last_trade or "N/A")
        if cols[6].button("Remove", key=f"remove-{wallet}"):
            remove_wallet_from_watchlist(wallet)
            st.rerun()


def render_watchlist_page(ranked_df: pd.DataFrame | None = None, whale_mode: bool = True) -> None:
    render_compact_title("Watchlist", badge("Private", "blue"))
    ensure_watchlist_state()
    watchlist_items = st.session_state["watchlist_items"]
    settings_state = user_settings()

    controls = st.columns([1, 1, 1])
    auto_refresh = controls[0].checkbox("Auto-refresh", value=True)
    alert_min_trade_value = controls[1].number_input(
        "Alert minimum trade size",
        min_value=0.0,
        value=float(settings_state["min_trade_size"]),
        step=50.0,
    )
    refresh_now = controls[2].button("Refresh Watchlist Trades")
    enable_popup_alerts = bool(settings_state["popup_alerts_enabled"])
    play_sound = False
    refresh_seconds = 60
    update_user_settings(min_trade_size=float(alert_min_trade_value))

    if not watchlist_items:
        render_empty_state("No wallets in watchlist yet", "Add wallets from the leaderboard to build your private whale monitor.")
        return

    if refresh_now:
        cached_wallet_recent_trades.clear()
        sync_user_data_from_firebase(force=True)
        st.rerun()

    ranked_lookup = {}
    if ranked_df is not None and not ranked_df.empty and "wallet" in ranked_df.columns:
        ranked_lookup = {str(row["wallet"]).lower(): row.to_dict() for _, row in ranked_df.iterrows()}

    if auto_refresh and hasattr(st, "fragment"):
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
        render_watchlist_body(
            ranked_lookup,
            whale_mode,
            float(alert_min_trade_value),
            enable_popup_alerts,
            play_sound,
        )


def crypto_watchlist_item_map() -> dict[str, dict]:
    return {
        str(item.get("wallet", "")).lower(): item
        for item in st.session_state.get("crypto_watchlist_items", [])
        if item.get("wallet")
    }


def crypto_wallet_tier(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet_key = str(wallet or "").lower()
    ranked_lookup = ranked_lookup or {}
    ranked_row = ranked_lookup.get(wallet_key, {})
    item = crypto_watchlist_item_map().get(wallet_key, {})
    return normalize_tier_name(
        ranked_row.get("whale_tier")
        or item.get("whale_tier")
        or item.get("wallet_label")
    )


def crypto_wallet_label_text(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet = str(wallet or "")
    return f"[{crypto_wallet_tier(wallet, ranked_lookup)}] {wallet}"


def crypto_wallet_badge_html(wallet: str, ranked_lookup: dict[str, dict] | None = None) -> str:
    wallet = str(wallet or "")
    tier = crypto_wallet_tier(wallet, ranked_lookup)
    return (
        f'<span class="ww-wallet-inline">{tier_html(tier)}'
        f'<span class="ww-wallet-address compact">{html.escape(wallet)}</span></span>'
    )


CRYPTO_TIMESTAMP_FIELDS = ["timestamp_raw", "timestamp", "timeStamp", "datetime", "blockTime", "created_at"]


def crypto_timestamp_to_raw(value) -> int:
    if value is None:
        return 0
    try:
        if pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return 0
        if number > 10_000_000_000:
            number = number / 1000
        return max(0, int(number))
    text = str(value).strip()
    if not text:
        return 0
    try:
        if text.startswith("0x"):
            return int(text, 16)
        number = float(text)
        if number > 10_000_000_000:
            number = number / 1000
        return max(0, int(number))
    except (TypeError, ValueError):
        pass
    try:
        parsed = pd.to_datetime(text, utc=True, errors="coerce")
        if pd.isna(parsed):
            return 0
        return max(0, int(parsed.timestamp()))
    except (TypeError, ValueError, OverflowError):
        return 0


def crypto_row_timestamp_raw(row: dict | pd.Series) -> int:
    for field in CRYPTO_TIMESTAMP_FIELDS:
        if hasattr(row, "get") and field in row:
            timestamp_raw = crypto_timestamp_to_raw(row.get(field))
            if timestamp_raw:
                return timestamp_raw
    return 0


def normalize_crypto_activity_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    normalized = df.copy()
    if "timestamp_raw" not in normalized.columns:
        normalized["timestamp_raw"] = 0
    normalized["timestamp_raw"] = normalized.apply(crypto_row_timestamp_raw, axis=1)
    if "timestamp" not in normalized.columns:
        normalized["timestamp"] = normalized["timestamp_raw"].apply(
            lambda value: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(value))) if int(value or 0) else ""
        )
    return normalized


def safe_sort_crypto_activity(df: pd.DataFrame | None, ascending: bool = False) -> pd.DataFrame:
    normalized = normalize_crypto_activity_df(df)
    if normalized.empty:
        return normalized
    sort_col = "timestamp_raw" if "timestamp_raw" in normalized.columns else None
    if sort_col is None:
        return normalized
    return normalized.sort_values(sort_col, ascending=ascending)


def build_crypto_activity_events_resilient(wallet: str, activity: list[dict], chain: str) -> list[dict]:
    try:
        from polymarket_tracker import crypto as crypto_module

        builder = getattr(crypto_module, "build_crypto_activity_events", None)
        if builder:
            return builder(wallet, activity, chain)
    except Exception:
        logger.warning("Falling back to local crypto activity grouping", exc_info=True)

    wallet = str(wallet).lower()
    groups: dict[str, list[dict]] = {}
    for transfer in activity:
        tx_hash = str(transfer.get("hash") or "")
        if tx_hash:
            groups.setdefault(tx_hash, []).append(transfer)
    events = []
    for tx_hash, rows in groups.items():
        incoming = [row for row in rows if str(row.get("to") or "").lower() == wallet and float(row.get("amount") or 0) > 0]
        outgoing = [row for row in rows if str(row.get("from") or "").lower() == wallet and float(row.get("amount") or 0) > 0]
        timestamp_raw = max(crypto_row_timestamp_raw(row) for row in rows)
        timestamp = pd.to_datetime(timestamp_raw, unit="s", utc=True).strftime("%Y-%m-%d %H:%M UTC") if timestamp_raw else ""
        if incoming and outgoing:
            sold = max(outgoing, key=lambda row: float(row.get("value_usd") or row.get("amount") or 0))
            bought = max(incoming, key=lambda row: float(row.get("value_usd") or row.get("amount") or 0))
            sold_token = str(sold.get("token") or "UNKNOWN")
            bought_token = str(bought.get("token") or "UNKNOWN")
            value = max(float(sold.get("value_usd") or 0), float(bought.get("value_usd") or 0))
            description = f"Possible swap: Swapped {sold_token} -> {bought_token}"
            events.append(
                {
                    "wallet": wallet,
                    "chain": chain,
                    "timestamp_raw": timestamp_raw,
                    "timestamp": timestamp,
                    "category": "Trades / Swaps",
                    "action": "SWAP",
                    "description": description,
                    "token_sold": sold_token,
                    "token_bought": bought_token,
                    "amount_sold": sold.get("amount") or 0,
                    "amount_bought": bought.get("amount") or 0,
                    "dollar_value": round(value, 2),
                    "tx_hash": tx_hash,
                    "explorer_link": "",
                    "confidence": "Possible swap",
                }
            )
            continue
        transfer = (incoming or outgoing or rows)[0]
        direction = "Received" if incoming else "Sent" if outgoing else "Transfer"
        token = str(transfer.get("token") or "")
        value = float(transfer.get("value_usd") or 0)
        events.append(
            {
                "wallet": wallet,
                "chain": chain,
                "timestamp_raw": timestamp_raw,
                "timestamp": timestamp,
                "category": "Large Transfers" if value >= 10000 else "Unknown Activity",
                "action": "RECEIVE" if direction == "Received" else "SEND" if direction == "Sent" else "TRANSFER",
                "description": f"{direction} {token}",
                "token_sold": token if direction == "Sent" else "",
                "token_bought": token if direction == "Received" else "",
                "amount_sold": transfer.get("amount") if direction == "Sent" else 0,
                "amount_bought": transfer.get("amount") if direction == "Received" else 0,
                "dollar_value": round(value, 2),
                "tx_hash": tx_hash,
                "explorer_link": "",
                "confidence": "Transfer",
            }
        )
    return sorted(events, key=lambda row: (0 if row.get("category") == "Trades / Swaps" else 1, -crypto_row_timestamp_raw(row)))


def crypto_rows_for_wallets(
    wallets: list[str],
    chain: str,
    token_filter: str,
    time_period_days: int,
    min_transaction_size: float,
) -> list[dict]:
    api_key = crypto_api_key(chain)
    rows = []
    progress_text = st.empty()
    progress_bar = st.progress(0)
    total = len(wallets)
    for index, wallet in enumerate(wallets, start=1):
        progress_text.markdown(f"Analyzing crypto wallet {index}/{total}")
        try:
            activity = cached_crypto_wallet_activity(wallet, chain, api_key, token_filter, int(time_period_days))
            row = calculate_crypto_wallet_score(wallet, activity, chain, float(min_transaction_size))
            rows.append(row)
        except CryptoAPIError as exc:
            logger.warning("Skipped crypto wallet %s", wallet, exc_info=True)
            st.warning(f"{wallet}: explorer data unavailable; skipped. {exc}")
        progress_bar.progress(index / max(total, 1))
    progress_text.empty()
    progress_bar.empty()
    return sorted(rows, key=lambda row: row.get("whale_score", 0), reverse=True)


def crypto_activity_dataframe(
    wallet: str,
    chain: str,
    token_filter: str,
    time_period_days: int,
    ranked_lookup: dict[str, dict] | None = None,
) -> pd.DataFrame:
    api_key = crypto_api_key(chain)
    try:
        activity = cached_crypto_recent_trades(wallet, chain, api_key, token_filter, int(time_period_days))
    except CryptoAPIError as exc:
        logger.warning("Could not load crypto activity for %s", wallet, exc_info=True)
        st.warning(f"Crypto activity unavailable for this wallet: {exc}")
        return pd.DataFrame()
    events = build_crypto_activity_events_resilient(wallet, activity, chain)
    rows = []
    for event in events:
        row = dict(event)
        row["wallet_label"] = crypto_wallet_label_text(wallet, ranked_lookup)
        rows.append(row)
    return safe_sort_crypto_activity(pd.DataFrame(rows)) if rows else pd.DataFrame()


def render_crypto_selected_wallet_panel(selected_wallet: str, selected_metadata: dict | None) -> None:
    if not selected_metadata:
        return
    lookup = {selected_wallet.lower(): selected_metadata}
    confidence = str(selected_metadata.get("confidence") or "Low")
    copy_quality = str(selected_metadata.get("copy_quality") or "Unproven")
    quality_tone = {"Elite": "green", "Strong": "green", "Risky": "red", "Unproven": "blue"}.get(copy_quality, "blue")
    confidence_tone = {"High": "green", "Medium": "blue", "Low": "red"}.get(confidence, "blue")
    reasons = selected_metadata.get("copy_reasons") or []
    if isinstance(reasons, str):
        reasons = [text.strip() for text in reasons.split(";") if text.strip()]
    reason_html = "".join(badge(html.escape(str(reason)), "blue") for reason in reasons[:4])
    st.markdown(
        f"""
        <div class="ww-wallet-panel">
          <div class="ww-brand-row">
            <div>
              <div class="ww-eyebrow">Selected Crypto Wallet</div>
              <div style="margin: 0.35rem 0;">{crypto_wallet_badge_html(selected_wallet, lookup)}</div>
            </div>
            <div class="ww-pill-row">
              {badge(str(selected_metadata.get("chain") or "On-chain"), "blue")}
              {badge(copy_quality, quality_tone)}
              {badge(confidence + " confidence", confidence_tone)}
            </div>
          </div>
          <div class="ww-terminal-line"></div>
          <div class="ww-section-copy">{html.escape(str(selected_metadata.get("profit_note") or "Crypto scoring uses public on-chain wallet activity only."))}</div>
          <div class="ww-pill-row" style="margin-top: 0.8rem;">{reason_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    metric_cols = st.columns(5)
    with metric_cols[0]:
        render_metric_card("Whale Score", f"{safe_number(selected_metadata.get('whale_score')):.2f}", "On-chain rank", "green")
    with metric_cols[1]:
        render_metric_card(
            "Profitable Trade %",
            format_optional_percent(crypto_profitable_trade_pct_value(selected_metadata)),
            "Completed cycles only",
            "green",
        )
    with metric_cols[2]:
        selected_roi = crypto_estimated_roi_value(selected_metadata)
        render_metric_card("Estimated ROI", format_optional_percent(selected_roi), "Decoded swap cycles", "green" if safe_number(selected_roi) >= 0 else "red")
    with metric_cols[3]:
        selected_profit = crypto_estimated_profit_value(selected_metadata)
        render_metric_card("Net Profit", format_optional_money(selected_profit), "Decoded cycle estimate", "green" if safe_number(selected_profit) >= 0 else "red")
    with metric_cols[4]:
        render_metric_card("Completed Cycles", f"{int(safe_number(selected_metadata.get('completed_trades'))):,}", "Buy then sell cycles", "blue")
    detail_cols = st.columns(5)
    with detail_cols[0]:
        render_metric_card("Avg Trade Size", format_money(selected_metadata.get("avg_trade_size")), "Mean swap value", "blue")
    with detail_cols[1]:
        avg_profit = selected_metadata.get("avg_profit_per_completed_trade") if crypto_completed_cycles(selected_metadata) else None
        render_metric_card("Avg Profit / Trade", format_optional_money(avg_profit), "Per completed cycle", "green" if safe_number(avg_profit) >= 0 else "red")
    with detail_cols[2]:
        frequency = optional_metric_number(selected_metadata.get("trading_frequency")) if crypto_completed_cycles(selected_metadata) else None
        render_metric_card("Trading Frequency", f"{frequency:.2f}/day" if frequency is not None else "Insufficient data", "Completed cycles", "blue")
    with detail_cols[3]:
        render_metric_card("Recent Profitable", f"{int(safe_number(selected_metadata.get('recent_profitable_trades'))):,}", "Last 7 days", "green")
    with detail_cols[4]:
        render_metric_card("Consistency", f"{safe_number(selected_metadata.get('consistency_score')):.2f}", "Repeatability score", "green")
    st.caption("Copy wallet address")
    st.code(selected_wallet, language="text")
    action_cols = st.columns([1, 1, 3])
    if action_cols[0].button(
        "Add to Crypto Watchlist",
        disabled=selected_wallet in st.session_state["crypto_watchlist"],
        key="add-selected-crypto-wallet",
    ):
        add_crypto_wallet_to_watchlist(selected_wallet, selected_metadata)
        st.rerun()
    if action_cols[1].button("View Recent Swaps", key="selected-crypto-wallet-recent-transfers"):
        st.session_state["selected_crypto_wallet"] = selected_wallet
        st.session_state["show_selected_crypto_trades"] = selected_wallet
        st.rerun()


def render_crypto_activity_table(trade_df: pd.DataFrame) -> None:
    trade_df = normalize_crypto_activity_df(trade_df)
    if trade_df.empty:
        render_empty_state("No matching activity", "No public on-chain swaps or transfers matched these filters.")
        return
    defaults = {
        "wallet_label": "",
        "chain": "",
        "timestamp": "",
        "category": "",
        "action": "",
        "description": "",
        "token_sold": "",
        "token_bought": "",
        "amount_sold": 0.0,
        "amount_bought": 0.0,
        "dollar_value": 0.0,
        "confidence": "",
        "tx_hash": "",
        "explorer_link": "",
    }
    for column, default in defaults.items():
        if column not in trade_df.columns:
            trade_df[column] = default
    display_df = trade_df[
        [
            "wallet_label",
            "chain",
            "timestamp",
            "category",
            "action",
            "description",
            "token_sold",
            "token_bought",
            "amount_sold",
            "amount_bought",
            "dollar_value",
            "confidence",
            "tx_hash",
            "explorer_link",
        ]
    ].copy()
    st.dataframe(
        style_financial_table(display_df),
        use_container_width=True,
        hide_index=True,
        column_config={
            "wallet_label": st.column_config.TextColumn("Wallet", width=430),
            "chain": "Chain",
            "timestamp": "Timestamp",
            "category": "Category",
            "action": "Action",
            "description": st.column_config.TextColumn("Activity", width=260),
            "token_sold": "Token sold",
            "token_bought": "Token bought",
            "amount_sold": st.column_config.NumberColumn("Amount sold", format="%.4f"),
            "amount_bought": st.column_config.NumberColumn("Amount bought", format="%.4f"),
            "dollar_value": st.column_config.NumberColumn("Estimated value", format="$%.2f"),
            "confidence": "Confidence",
            "tx_hash": st.column_config.TextColumn("Transaction", width=260),
            "explorer_link": st.column_config.LinkColumn("Explorer", display_text="Open"),
        },
    )


def render_crypto_wallet_details(
    chain: str,
    token_filter: str,
    time_period_days: int,
    ranked_lookup: dict[str, dict] | None = None,
) -> None:
    ensure_crypto_watchlist_state()
    ranked_lookup = ranked_lookup or {}
    wallet_options = st.session_state["crypto_watchlist"]
    render_compact_title("Crypto Wallet Details", badge("On-chain", "blue"))
    if not wallet_options:
        render_empty_state("Wallet Details", "Add wallets to your crypto watchlist before viewing wallet details.")
        return
    selected = str(st.session_state.get("selected_crypto_wallet") or "")
    selected_index = wallet_options.index(selected) if selected in wallet_options else 0
    viewer_wallet = st.selectbox(
        "Select wallet",
        wallet_options,
        index=selected_index,
        format_func=lambda wallet: crypto_wallet_label_text(wallet, ranked_lookup),
        key="crypto_trade_viewer_wallet",
    )
    st.markdown(crypto_wallet_badge_html(viewer_wallet, ranked_lookup), unsafe_allow_html=True)
    filter_cols = st.columns([1, 1, 1])
    activity_filter = filter_cols[0].selectbox("Activity type", ["Swaps only", "Transfers only", "All activity"])
    min_value = filter_cols[1].number_input("Minimum value", min_value=0.0, value=0.0, step=500.0)
    lookback_label = filter_cols[2].selectbox("Time window", ["Last 24h", "Last 7d", "Last 30d", "Selected period"], index=3)
    lookback_days = {"Last 24h": 1, "Last 7d": 7, "Last 30d": 30, "Selected period": int(time_period_days)}[lookback_label]
    trade_df = crypto_activity_dataframe(viewer_wallet, chain, token_filter, lookback_days, ranked_lookup)
    trade_df = normalize_crypto_activity_df(trade_df)
    if not trade_df.empty:
        if "dollar_value" not in trade_df.columns:
            trade_df["dollar_value"] = 0.0
        if "category" not in trade_df.columns:
            trade_df["category"] = ""
        trade_df = trade_df[pd.to_numeric(trade_df["dollar_value"], errors="coerce").fillna(0) >= float(min_value)]
        if activity_filter == "Swaps only":
            trade_df = trade_df[trade_df["category"] == "Trades / Swaps"]
        elif activity_filter == "Transfers only":
            trade_df = trade_df[trade_df["category"].isin(["Large Transfers", "Deposits / Withdrawals"])]
    render_crypto_activity_table(trade_df)


def detect_crypto_alerts(activity_df: pd.DataFrame, min_trade_value: float, ranked_lookup: dict[str, dict]) -> list[dict]:
    seen_ids = {str(item) for item in st.session_state.get("crypto_seen_trade_ids", [])}
    seen_timestamps = {
        str(key): int(value or 0)
        for key, value in st.session_state.get("crypto_seen_trade_timestamps", {}).items()
    }
    initialized_wallets = {
        str(wallet).lower()
        for wallet in st.session_state.get("crypto_alert_wallets_initialized", [])
    }
    activity_df = normalize_crypto_activity_df(activity_df)
    if activity_df.empty:
        initialized_wallets.update(st.session_state["crypto_watchlist"])
        st.session_state["crypto_alert_wallets_initialized"] = sorted(initialized_wallets)
        return []
    for column, default in {"wallet": "", "tx_hash": "", "dollar_value": 0.0, "category": ""}.items():
        if column not in activity_df.columns:
            activity_df[column] = default
    activity_df["dollar_value"] = pd.to_numeric(activity_df["dollar_value"], errors="coerce").fillna(0)
    new_alerts = []
    for wallet in st.session_state["crypto_watchlist"]:
        wallet_rows = activity_df[activity_df["wallet"].astype(str).str.lower() == wallet] if "wallet" in activity_df.columns else pd.DataFrame()
        if wallet not in initialized_wallets:
            wallet_rows = normalize_crypto_activity_df(wallet_rows)
            for _, row in wallet_rows.iterrows():
                trade_id = str(row.get("tx_hash") or "")
                if trade_id:
                    seen_ids.add(trade_id)
                    seen_timestamps[trade_id] = crypto_row_timestamp_raw(row)
            initialized_wallets.add(wallet)
            continue
        unseen = wallet_rows[~wallet_rows["tx_hash"].astype(str).isin(seen_ids)] if not wallet_rows.empty else pd.DataFrame()
        if not unseen.empty:
            for column, default in {"dollar_value": 0.0, "category": ""}.items():
                if column not in unseen.columns:
                    unseen[column] = default
            unseen["dollar_value"] = pd.to_numeric(unseen["dollar_value"], errors="coerce").fillna(0)
            alert_categories = ["Trades / Swaps", "Large Transfers"]
            eligible = unseen[
                (unseen["dollar_value"] >= float(min_trade_value))
                & (unseen["category"].isin(alert_categories))
            ]
        else:
            eligible = pd.DataFrame()
        if eligible.empty:
            continue
        eligible = safe_sort_crypto_activity(eligible)
        sort_col = "timestamp_raw" if "timestamp_raw" in eligible.columns else None
        if sort_col is None:
            sorted_eligible = eligible
        else:
            sorted_eligible = eligible.sort_values(sort_col, ascending=False)
        for _, row in sorted_eligible.iterrows():
            alert = row.to_dict()
            alert["wallet_tier"] = crypto_wallet_tier(wallet, ranked_lookup)
            new_alerts.append(alert)
            st.toast(
                f"New on-chain whale activity: {crypto_wallet_label_text(wallet, ranked_lookup)} "
                f"{row.get('description')} ({format_money(row.get('dollar_value'))})"
            )
        for _, row in wallet_rows.iterrows():
            trade_id = str(row.get("tx_hash") or "")
            if trade_id:
                seen_ids.add(trade_id)
                seen_timestamps[trade_id] = crypto_row_timestamp_raw(row)
    st.session_state["crypto_seen_trade_ids"] = sorted(seen_ids)
    st.session_state["crypto_seen_trade_timestamps"] = seen_timestamps
    st.session_state["crypto_alert_wallets_initialized"] = sorted(initialized_wallets)
    if new_alerts:
        st.session_state["crypto_new_trade_alerts"] = (new_alerts + st.session_state.get("crypto_new_trade_alerts", []))[:50]
    return new_alerts


def render_crypto_new_alerts(ranked_lookup: dict[str, dict]) -> None:
    alerts = st.session_state.get("crypto_new_trade_alerts", [])
    render_compact_title("Crypto Alerts", badge(f"{len(alerts)} alert{'s' if len(alerts) != 1 else ''}", "green"))
    if not alerts:
        render_empty_state("No crypto alerts", "New watched-wallet transfers above your alert minimum will appear here.")
        return
    clear_col, count_col = st.columns([1, 4])
    if clear_col.button("Clear crypto alerts", key="clear-crypto-alerts"):
        st.session_state["crypto_new_trade_alerts"] = []
        st.rerun()
    count_col.caption("Newest first")
    for alert in alerts[:12]:
        wallet = str(alert.get("wallet") or "")
        value = safe_number(alert.get("dollar_value"))
        st.markdown(
            f"""
            <div class="ww-alert-row">
              <div class="ww-brand-row">
                <div>{crypto_wallet_badge_html(wallet, ranked_lookup)}</div>
                <div class="ww-num ww-num-positive">{format_money(value)}</div>
              </div>
              <div class="ww-alert-market">{html.escape(str(alert.get("description") or "On-chain activity"))}</div>
              <div class="ww-section-copy">{html.escape(str(alert.get("category") or ""))} · {html.escape(str(alert.get("confidence") or ""))} · {html.escape(str(alert.get("timestamp") or ""))}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def crypto_last_activity_from_rows(rows: list[dict]) -> str:
    timestamps = [crypto_row_timestamp_raw(row) for row in rows if crypto_row_timestamp_raw(row) > 0]
    if not timestamps:
        return ""
    latest = max(timestamps)
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(latest))


def watchlist_metric_text(value, formatter, fallback: str = "Calculating...") -> str:
    number = optional_metric_number(value)
    if number is None:
        return fallback
    return formatter(number)


def hydrate_crypto_watchlist_metrics(
    chain: str,
    token_filter: str,
    time_period_days: int,
    min_transaction_size: float,
    ranked_lookup: dict[str, dict],
    last_times: dict[str, str],
) -> dict[str, str]:
    status: dict[str, str] = {}
    api_keys: dict[str, str | None] = {}
    for item in list(st.session_state.get("crypto_watchlist_items", [])):
        wallet = str(item.get("wallet") or "").lower()
        if not wallet:
            continue
        merged = {**item, **ranked_lookup.get(wallet, {})}
        if last_times.get(wallet):
            merged["last_activity"] = last_times[wallet]
        if crypto_watchlist_has_core_metrics(merged):
            if not crypto_watchlist_has_core_metrics(item) or (last_times.get(wallet) and item.get("last_activity") != last_times[wallet]):
                persist_crypto_watchlist_metrics(wallet, merged, last_times.get(wallet))
            status[wallet] = "Ready"
            continue

        if wallet in ranked_lookup and crypto_watchlist_has_core_metrics(ranked_lookup[wallet]):
            persist_crypto_watchlist_metrics(wallet, ranked_lookup[wallet], last_times.get(wallet))
            status[wallet] = "Ready"
            continue

        wallet_chain = str(item.get("chain") or chain)
        if wallet_chain not in CHAIN_CONFIGS:
            wallet_chain = chain
        if wallet_chain not in api_keys:
            api_keys[wallet_chain] = crypto_api_key(wallet_chain)
        api_key = api_keys[wallet_chain]
        if not api_key:
            status[wallet] = "Calculating..."
            continue
        try:
            activity = cached_crypto_wallet_activity(wallet, wallet_chain, api_key, token_filter, int(time_period_days))
            row = calculate_crypto_wallet_score(wallet, activity, wallet_chain, float(min_transaction_size))
            last_activity = last_times.get(wallet) or crypto_last_activity_from_rows(activity)
            persist_crypto_watchlist_metrics(wallet, row, last_activity)
            ranked_lookup[wallet] = {**ranked_lookup.get(wallet, {}), **row}
            status[wallet] = "Ready"
        except CryptoAPIError:
            logger.warning("Could not hydrate crypto watchlist metrics for %s", wallet, exc_info=True)
            status[wallet] = "Unavailable"
    return status


def render_crypto_watchlist(
    chain: str,
    token_filter: str,
    time_period_days: int,
    alert_min_trade_value: float,
    ranked_lookup: dict[str, dict],
) -> None:
    ensure_crypto_watchlist_state()
    render_compact_title("Crypto Watchlist", badge("Public on-chain", "blue"))
    controls = st.columns([1, 1, 1])
    auto_refresh = controls[0].checkbox("Auto-refresh", value=True, key="crypto-auto-refresh")
    alert_min = controls[1].number_input("Alert minimum transfer", min_value=0.0, value=float(alert_min_trade_value), step=500.0)
    refresh_now = controls[2].button("Refresh Crypto Watchlist")
    if not st.session_state["crypto_watchlist_items"]:
        render_empty_state("No crypto wallets in watchlist yet", "Add crypto wallets from the leaderboard to monitor on-chain activity.")
        return
    if refresh_now:
        cached_crypto_recent_trades.clear()
        cached_crypto_wallet_activity.clear()
        sync_user_data_from_firebase(force=True)
        st.rerun()
    frames = []
    for wallet in st.session_state["crypto_watchlist"]:
        frame = crypto_activity_dataframe(wallet, chain, token_filter, int(time_period_days), ranked_lookup)
        if not frame.empty:
            frame["wallet"] = wallet
            frames.append(frame)
    activity_df = safe_sort_crypto_activity(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
    last_times = last_trade_times_from_df(activity_df.rename(columns={"dollar_value": "value"})) if not activity_df.empty else {}
    metric_status = hydrate_crypto_watchlist_metrics(
        chain,
        token_filter,
        int(time_period_days),
        float(alert_min_trade_value),
        ranked_lookup,
        last_times,
    )
    detect_crypto_alerts(activity_df, float(alert_min), ranked_lookup)
    render_crypto_new_alerts(ranked_lookup)
    render_compact_title("Saved Crypto Wallets")
    header = st.columns([1.2, 4.4, 1.15, 1.25, 1.25, 1.35, 1.35, 1.75, 1])
    for col, title in zip(
        header,
        ["Tier", "Wallet address", "Profitable Trade %", "Estimated ROI", "Whale Score", "Net Profit", "Total Volume", "Last Activity", "Remove"],
    ):
        col.markdown(f'<div class="ww-watchlist-head">{title}</div>', unsafe_allow_html=True)
    for item in list(st.session_state["crypto_watchlist_items"]):
        wallet = str(item.get("wallet", "")).lower()
        merged = {**item, **ranked_lookup.get(wallet, {})}
        fallback = metric_status.get(wallet, "Calculating...")
        last_activity = last_times.get(wallet) or str(merged.get("last_activity") or "")
        cols = st.columns([1.2, 4.4, 1.15, 1.25, 1.25, 1.35, 1.35, 1.75, 1])
        cols[0].markdown(tier_html(crypto_wallet_tier(wallet, ranked_lookup)), unsafe_allow_html=True)
        cols[1].markdown(f'<span class="ww-wallet-address compact">{html.escape(wallet)}</span>', unsafe_allow_html=True)
        cols[2].write(watchlist_metric_text(crypto_profitable_trade_pct_value(merged), format_percent, "Insufficient data"))
        cols[3].write(watchlist_metric_text(crypto_estimated_roi_value(merged), format_percent, "Insufficient data"))
        cols[4].write(watchlist_metric_text(merged.get("whale_score"), lambda value: f"{value:.2f}", fallback))
        cols[5].write(watchlist_metric_text(crypto_estimated_profit_value(merged), format_money, "Insufficient data"))
        cols[6].write(watchlist_metric_text(merged.get("total_volume"), format_money, fallback))
        cols[7].write(last_activity or fallback)
        if cols[8].button("Remove", key=f"remove-crypto-{wallet}"):
            remove_crypto_wallet_from_watchlist(wallet)
            st.rerun()
    if auto_refresh:
        components.html("<script>setTimeout(() => window.parent.location.reload(), 60000);</script>", height=0)


def render_crypto_dashboard(
    rows: list[dict],
    chain: str,
    min_volume: float,
    min_whale_score: float,
    min_win_rate: float,
    token_filter: str,
    time_period_days: int,
) -> None:
    render_section_header(
        "Crypto Whale Finder",
        "On-Chain Intelligence",
        "Crypto whale tracking using public on-chain wallet activity. Binance private user trades are not public and are not tracked.",
        right_html=f'{badge(chain, "blue")}{badge("Read-only", "green")}',
    )
    if not rows:
        render_empty_state(
            "No crypto whale scan loaded",
            "Use Discover Crypto Whales in the sidebar. Add explorer API keys or seed wallets for richer discovery.",
        )
        return
    filtered = []
    for row in rows:
        profitable_pct = crypto_profitable_trade_pct_value(row)
        passes_profitable_filter = float(min_win_rate) <= 0 or (
            profitable_pct is not None and profitable_pct >= float(min_win_rate)
        )
        if (
            safe_number(row.get("total_volume")) >= float(min_volume)
            and safe_number(row.get("whale_score")) >= float(min_whale_score)
            and passes_profitable_filter
        ):
            filtered.append(row)
    if not filtered:
        filtered = sorted(rows, key=lambda row: row.get("whale_score", 0), reverse=True)[: max(1, min(25, len(rows)))]
        st.info("Showing best available crypto wallets because no wallet passed every selected threshold.")
    df = pd.DataFrame(sorted(filtered, key=lambda row: row.get("whale_score", 0), reverse=True))
    columns = [
        "whale_tier",
        "wallet",
        "whale_score",
        "profitable_trade_pct",
        "roi_pct",
        "net_profit",
        "total_volume",
        "avg_trade_size",
        "completed_trades",
        "avg_profit_per_completed_trade",
        "recent_profitable_trades",
        "trading_frequency",
        "confidence",
        "copy_quality",
    ]
    for column in columns:
        if column not in df.columns:
            if column in {"whale_tier", "wallet", "confidence", "copy_quality"}:
                df[column] = ""
            elif column in {"profitable_trade_pct", "roi_pct", "net_profit", "avg_profit_per_completed_trade", "trading_frequency"}:
                df[column] = None
            else:
                df[column] = 0
    metric_cols = st.columns(4)
    with metric_cols[0]:
        render_metric_card("Wallets Found", f"{len(rows):,}", "Public candidates", "blue")
    with metric_cols[1]:
        render_metric_card("Shown", f"{len(df):,}", "After filters", "green")
    with metric_cols[2]:
        render_metric_card("Best Copy Score", f"{safe_number(df['whale_score'].max()):.2f}", "Quality-weighted rank", "green")
    with metric_cols[3]:
        best_roi = pd.to_numeric(df["roi_pct"], errors="coerce").max()
        render_metric_card("Top Estimated ROI", format_optional_percent(best_roi), "Completed cycles only", "green" if safe_number(best_roi) >= 0 else "red")
    display_df = df[columns].copy()
    completed_mask = pd.to_numeric(df["completed_trades"], errors="coerce").fillna(0) > 0
    display_df.loc[~completed_mask, ["profitable_trade_pct", "roi_pct", "net_profit", "avg_profit_per_completed_trade", "trading_frequency"]] = None
    display_df["profitable_trade_pct"] = display_df["profitable_trade_pct"].apply(format_optional_percent)
    display_df["roi_pct"] = display_df["roi_pct"].apply(format_optional_percent)
    display_df["net_profit"] = display_df["net_profit"].apply(format_optional_money)
    display_df["avg_profit_per_completed_trade"] = display_df["avg_profit_per_completed_trade"].apply(format_optional_money)
    display_df["trading_frequency"] = display_df["trading_frequency"].apply(
        lambda value: f"{safe_number(value):.2f}/day" if optional_metric_number(value) is not None else "Insufficient data"
    )
    ranking_event = st.dataframe(
        style_financial_table(display_df),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="crypto_wallet_rankings_table",
        column_config={
            "whale_tier": st.column_config.TextColumn("Tier", width=130),
            "wallet": st.column_config.TextColumn("Wallet Address", width=420, pinned=True),
            "whale_score": st.column_config.NumberColumn("Whale Score", format="%.2f", alignment="right"),
            "profitable_trade_pct": st.column_config.TextColumn("Profitable Trade %", width=170),
            "roi_pct": st.column_config.TextColumn("Estimated ROI", width=140),
            "net_profit": st.column_config.TextColumn("Net Profit", width=140),
            "total_volume": st.column_config.NumberColumn("Total Volume", format="$%.2f", alignment="right"),
            "avg_trade_size": st.column_config.NumberColumn("Average Trade Size", format="$%.2f", alignment="right"),
            "completed_trades": st.column_config.NumberColumn("Completed Trade Cycles", format="%d", alignment="right"),
            "avg_profit_per_completed_trade": st.column_config.TextColumn("Avg Profit / Trade", width=160),
            "recent_profitable_trades": st.column_config.NumberColumn("Recent Profitable Trades", format="%d", alignment="right"),
            "trading_frequency": st.column_config.TextColumn("Trading Frequency", width=150),
            "confidence": st.column_config.TextColumn("Confidence", width=130),
            "copy_quality": st.column_config.TextColumn("Copy Quality", width=140),
        },
    )
    selected_wallet = None
    if isinstance(ranking_event, dict):
        selected_rows = ranking_event.get("selection", {}).get("rows", [])
    else:
        selected_rows = getattr(getattr(ranking_event, "selection", None), "rows", [])
    if selected_rows:
        selected_wallet = str(df.iloc[selected_rows[0]]["wallet"])
        st.session_state["selected_crypto_wallet"] = selected_wallet
    elif str(st.session_state.get("selected_crypto_wallet") or "").lower() in set(df["wallet"].astype(str).str.lower()):
        selected_wallet = str(st.session_state["selected_crypto_wallet"])
    if selected_wallet:
        selected_row = df[df["wallet"].astype(str).str.lower() == selected_wallet.lower()]
        selected_metadata = selected_row.iloc[0].to_dict() if not selected_row.empty else None
        render_crypto_selected_wallet_panel(selected_wallet, selected_metadata)
        if st.session_state.get("show_selected_crypto_trades") == selected_wallet:
            ranked_lookup = {str(row["wallet"]).lower(): row.to_dict() for _, row in df.iterrows()}
            render_compact_title("Trades / Swaps")
            trade_df = crypto_activity_dataframe(selected_wallet, chain, token_filter, time_period_days, ranked_lookup)
            if not trade_df.empty:
                swaps_df = trade_df[trade_df["category"] == "Trades / Swaps"]
                trade_df = swaps_df if not swaps_df.empty else trade_df
            render_crypto_activity_table(trade_df)
    csv = df[columns].to_csv(index=False).encode("utf-8")
    st.download_button("Export crypto results to CSV", csv, "whalewatch_crypto_rankings.csv", "text/csv")


def render_crypto_platform(
    crypto_page: str,
    chain: str,
    min_volume: float,
    min_transaction_size: float,
    min_whale_score: float,
    min_win_rate: float,
    token_filter: str,
    time_period_days: int,
    max_wallets: int,
    include_cex_related: bool,
    seed_wallet_text: str,
    discover_crypto: bool,
) -> None:
    ensure_crypto_watchlist_state()
    api_key = crypto_api_key(chain)
    seed_wallets = tuple(normalize_wallets(seed_wallet_text))
    if discover_crypto:
        if not api_key:
            st.error(f"{chain} scanning needs an explorer API key in `.streamlit/secrets.toml`.")
        else:
            try:
                with st.spinner("Scanning public on-chain transfers..."):
                    wallets = cached_discover_crypto_whales(
                        chain,
                        api_key,
                        token_filter,
                        float(min_transaction_size),
                        int(max_wallets),
                        int(time_period_days),
                        bool(include_cex_related),
                        seed_wallets,
                    )
                    st.session_state["crypto_discovered_wallets"] = wallets
                    st.session_state["crypto_rows"] = crypto_rows_for_wallets(
                        wallets,
                        chain,
                        token_filter,
                        int(time_period_days),
                        float(min_transaction_size),
                    )
                if wallets:
                    st.success("Fetched public on-chain activity successfully.")
                else:
                    st.warning("No crypto whale candidates found. Lower the transfer threshold or try a shorter time window/token.")
            except CryptoAPIError as exc:
                logger.warning("Crypto discovery failed", exc_info=True)
                st.error(f"Crypto scanner stopped: {exc}")
    rows = st.session_state.get("crypto_rows", [])
    ranked_lookup = {str(row["wallet"]).lower(): row for row in rows if row.get("wallet")}
    if crypto_page == "Watchlist":
        render_crypto_watchlist(chain, token_filter, int(time_period_days), float(min_transaction_size), ranked_lookup)
        return
    if crypto_page == "Wallet Details":
        render_crypto_wallet_details(chain, token_filter, int(time_period_days), ranked_lookup)
        return
    render_crypto_dashboard(
        rows,
        chain,
        float(min_volume),
        float(min_whale_score),
        float(min_win_rate),
        token_filter,
        int(time_period_days),
    )


if not current_auth_session():
    render_auth_page()
    st.stop()

sync_user_data_from_firebase()
auth_session = current_auth_session()
settings_state = user_settings()

user_email = html.escape(str(auth_session.get("email", "unknown") if auth_session else "unknown"))
render_brand_header(
    compact=True,
    right_html=(
        f'<div class="ww-pill-row">{badge("Live terminal", "green")}'
        f'<span class="ww-user-pill">{user_email}</span></div>'
    ),
)

market_tab = st.radio(
    "Market",
    ["Polymarket", "Crypto"],
    horizontal=True,
    label_visibility="collapsed",
    key="market_tab",
)

with st.sidebar:
    st.markdown(
        f"""
        <div class="ww-pill-row" style="margin-bottom:0.65rem;">
          {badge("Read-only", "green")}
          {badge("Private watchlist", "blue")}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Logged in as {auth_session.get('email', 'unknown') if auth_session else 'unknown'}")
    if st.button("Logout"):
        sign_out()
    if market_tab == "Crypto":
        crypto_page = st.radio("Crypto", ["Dashboard", "Wallet Details", "Watchlist"])
        st.header("Crypto Discovery")
        crypto_chain = st.selectbox("Chain", list(CHAIN_CONFIGS.keys()))
        token_options = ["All"] + list(CHAIN_CONFIGS[crypto_chain]["stable_tokens"].keys())
        crypto_token_filter = st.selectbox("Token filter", token_options)
        crypto_time_period_days = st.selectbox("Time period", [7, 30, 90], index=1, format_func=lambda d: f"Last {d} days")
        crypto_max_wallets = st.number_input("Candidate wallets to analyze", min_value=5, max_value=200, value=50, step=5)
        st.checkbox(
            "Exclude exchange/contract wallets",
            value=True,
            disabled=True,
            help="Obvious non-trader addresses such as zero/burn addresses, labelled exchange hot wallets, DEX routers, and known token contracts are not ranked.",
        )
        include_cex_related = False
        crypto_seed_wallet_text = st.text_area(
            "Optional seed wallets",
            height=100,
            placeholder="0x...\n0x...",
            help="Optional public wallets to score alongside discovered candidates.",
        )
        discover_crypto = st.button("Discover Crypto Whales", type="primary")
        st.header("Crypto Filters")
        crypto_min_volume = st.number_input("Minimum wallet volume", min_value=0.0, value=25000.0, step=5000.0)
        crypto_min_transaction_size = st.number_input("Minimum transaction size", min_value=0.0, value=25000.0, step=5000.0)
        crypto_min_whale_score = st.slider("Minimum whale score", 0, 100, 20, 5)
        crypto_min_win_rate = st.number_input(
            "Minimum profitable trade %",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=1.0,
            help="Calculated only from completed public buy/sell swap cycles. Wallets without completed cycles show insufficient data.",
        )
    else:
        page = st.radio("Polymarket", ["Dashboard", "Wallet Details", "Watchlist"])
        st.header("Discovery")
        whale_mode = st.checkbox(
            "Whale Mode",
            value=bool(settings_state["whale_mode_enabled"]),
            help="Prioritizes wallets with meaningful capital, larger trade sizes, and cleaner non-bot behavior.",
        )
        update_user_settings(whale_mode_enabled=bool(whale_mode))
        fast_mode = st.checkbox(
            "Fast Mode",
            value=not whale_mode,
            help="Uses recent trades only for a quick ranked view. Disable it for deeper historical metrics.",
        )
        market_category = st.selectbox("Market category", list(CATEGORY_KEYWORDS.keys()))
        time_period_days = st.selectbox("Time period", [30, 90, 180], index=1, format_func=lambda d: f"Last {d} days")
        max_recent_trades = st.number_input(
            "Recent trades to scan",
            min_value=500,
            max_value=5000,
            value=DEFAULT_RECENT_TRADES_TO_SCAN,
            step=100,
        )
        max_wallets = st.number_input(
            "Candidate wallets to analyze",
            min_value=5,
            max_value=200,
            value=MAX_WALLETS_FAST,
            step=5,
        )
        max_runtime_seconds = st.number_input(
            "Max runtime seconds",
            min_value=30,
            max_value=600,
            value=DEFAULT_MAX_RUN_SECONDS,
            step=30,
            help="Stops long scans after this many seconds and shows partial results.",
        )
        max_api_calls_per_run = st.number_input(
            "Max API calls per run",
            min_value=25,
            max_value=1000,
            value=DEFAULT_MAX_API_CALLS_PER_RUN,
            step=25,
            help="Stops a run after this many wallet/API attempts.",
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
            min_whale_score = st.slider("Minimum whale score", 0, 100, 20, 5, help="Soft quality floor. If no wallets pass, WhaleWatch still shows the best available ranked wallets.")
            min_net_profit = st.slider("Minimum net profit", -5000, 100000, 500, 500, help="Profit preference. Strong volume can still keep a wallet in the ranked fallback set.")
            min_volume = st.slider("Minimum total volume", 0, 1000000, 10000, 5000, help="Total observed dollars traded by the wallet.")
            min_avg_trade_size = st.slider("Minimum average trade size", 0, 10000, 150, 50, help="Raises the capital threshold to reduce tiny-wallet noise.")
            min_largest_trade = st.slider("Minimum largest trade", 0, 100000, 500, 500, help="Requires at least one position large enough to matter.")
            min_resolved = st.slider("Minimum resolved markets", 0, 250, 8, 1, help="Moderate sample-size guardrail for reliability.")
        else:
            min_whale_score = 0
            min_net_profit = 0
            min_avg_trade_size = 0
            min_largest_trade = 0
            min_resolved = st.number_input("Minimum resolved markets", min_value=0, value=5 if fast_mode else 50, step=5)
            min_volume = st.number_input("Minimum total volume", min_value=0.0, value=100.0 if fast_mode else 1000.0, step=250.0)
        min_win_rate = st.number_input("Minimum win rate %", min_value=0.0, max_value=100.0, value=45.0, step=1.0, help="Raw resolved-market win rate threshold.")
        min_roi = st.number_input("Minimum ROI %", min_value=-100.0, value=-10.0 if whale_mode else 0.0, step=1.0, help="Profit relative to capital deployed where historical data is available.")
        min_unique_markets = st.number_input("Minimum unique markets", min_value=0, value=0, step=1, help="Diversification guardrail across distinct markets.")
        include_aggressive_traders = st.checkbox("Include aggressive traders", value=True, help="Keeps high-volume, high-position-size wallets even when ROI is volatile.")
        include_bots = st.checkbox("Include bots", value=False, help="When off, wallets with strong bot-like repetition are heavily filtered after score penalties.")
        exclude_lucky = st.checkbox("Down-rank one lucky big win", value=True, help="Large single-win concentration lowers score instead of fully removing the wallet.")
        exclude_low_liq = st.checkbox("Exclude weak trade-data liquidity", value=True, help="Uses trade-data proxies rather than fragile open-interest endpoints.")
        include_timing = st.checkbox("Estimate entry timing from price history", value=False, disabled=fast_mode, help="Optional and best-effort. Ranking still works when price history is unavailable.")
        analyze = st.button("Analyze manual wallets")

if market_tab == "Crypto":
    render_crypto_platform(
        crypto_page,
        crypto_chain,
        float(crypto_min_volume),
        float(crypto_min_transaction_size),
        float(crypto_min_whale_score),
        float(crypto_min_win_rate),
        crypto_token_filter,
        int(crypto_time_period_days),
        int(crypto_max_wallets),
        bool(include_cex_related),
        crypto_seed_wallet_text,
        bool(discover_crypto),
    )
    st.stop()

wallets = normalize_wallets(wallet_text)

if page == "Dashboard":
    render_section_header(
        "Whale Discovery",
        "Market Intelligence",
        "Scan public prediction-market flow, isolate high-capital wallets, and rank copyability signals.",
        right_html=f'{badge("Whale Mode" if whale_mode else "Standard Mode", "green" if whale_mode else "blue")}',
    )

if discover:
    scan_status = None
    scan_progress = None
    try:
        run_started_at = time.time()
        scan_status = st.empty()
        scan_progress = st.progress(0, "Scanning recent trades...")
        with st.spinner("Scanning live market flow..."):
            wallets, recent_trades = discover_wallets_from_recent_trades(
                int(time_period_days),
                str(market_category),
                int(max_recent_trades),
                int(max_wallets),
                int(max_runtime_seconds),
                progress=scan_progress,
                status=scan_status,
                started_at=run_started_at,
            )
        scan_status.empty()
        scan_progress.empty()
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
        if scan_status:
            scan_status.empty()
        if scan_progress:
            scan_progress.empty()
        logger.warning("Could not discover wallets", exc_info=exc)
        st.error("Discovery stopped: API error.")
        wallets = []

if analyze and wallets:
    st.session_state["discovered_wallets"] = wallets

candidates = st.session_state.get("discovered_wallets", wallets)[: int(max_wallets)]
st.markdown(
    f"""
    <div class="ww-pill-row" style="margin: 0.4rem 0 1rem;">
      <span class="ww-badge ww-badge-blue"><span class="ww-live"></span>{len(candidates)} candidate wallets armed</span>
    </div>
    """,
    unsafe_allow_html=True,
)

if (discover and not fast_mode) or analyze:
    rows = []
    price_history_warning = False
    progress_text = st.empty()
    progress_bar = st.progress(0)
    run_started_at = time.time()
    analyzed_wallets = candidates[: int(max_wallets)]
    total_wallets = len(analyzed_wallets)
    stop_reason = ""
    if not analyzed_wallets:
        stop_reason = "no more candidate wallets"
    else:
        for index, wallet in enumerate(analyzed_wallets, start=1):
            elapsed = time.time() - run_started_at
            if index > int(max_api_calls_per_run):
                stop_reason = "max API calls reached"
                break
            if elapsed > int(max_runtime_seconds):
                stop_reason = "max runtime reached"
                break
            progress_text.markdown(f"Analyzing wallet {index}/{total_wallets}")
            try:
                row = analyze_wallet(wallet, include_timing, int(time_period_days), str(market_category))
                price_history_warning = price_history_warning or bool(row.get("price_history_warning"))
                rows.append(row)
            except PolymarketAPIError as exc:
                logger.warning("Skipped wallet %s", wallet, exc_info=exc)
                st.warning(f"{wallet}: public API data unavailable; skipped.")
                if not rows:
                    stop_reason = "API error"
            progress_bar.progress(index / total_wallets)
        else:
            stop_reason = "user selected limit reached"
    progress_text.empty()
    progress_bar.empty()
    if stop_reason == "user selected limit reached" and total_wallets < int(max_wallets):
        stop_reason = "no more candidate wallets"
    if stop_reason and stop_reason != "user selected limit reached":
        st.info(f"Showing partial results: {stop_reason}.")
    elif stop_reason:
        st.success(f"Analysis complete: {stop_reason}.")
    st.session_state["rows"] = rows
    st.session_state["fast_mode_results"] = False
    st.session_state["price_history_warning"] = price_history_warning

rows = st.session_state.get("rows", [])
if not rows:
    if page == "Watchlist":
        render_watchlist_page()
        st.stop()
    if page == "Wallet Details":
        render_wallet_trade_viewer(
            st.session_state["watchlist"],
            st.session_state.get("selected_wallet"),
            require_watchlist=True,
        )
        st.stop()
    render_empty_state(
        "No whale scan loaded",
        "Use Discover Wallets to scan recent public trade flow, or paste known wallets in the sidebar and run a manual analysis.",
    )
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
    filtered, whale_filter_reason = rank_whale_rows(
        rows,
        float(min_whale_score),
        float(min_net_profit),
        float(min_volume),
        float(min_win_rate),
        float(min_roi),
        float(min_avg_trade_size),
        float(min_largest_trade),
        int(min_resolved),
        bool(include_aggressive_traders),
        bool(include_bots),
        bool(st.session_state.get("fast_mode_results")),
        int(max_wallets),
    )
    if whale_filter_reason != "preferred thresholds" and whale_filter_reason != "no usable data exists":
        st.info(f"Showing ranked fallback: {whale_filter_reason}.")
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
    detail_lookup = {str(row["wallet"]).lower(): row.to_dict() for _, row in df.iterrows()} if "wallet" in df.columns else {}
    selected_detail_wallet = st.session_state.get("selected_wallet")
    if str(selected_detail_wallet or "").lower() not in set(st.session_state["watchlist"]):
        selected_detail_wallet = None
    render_wallet_trade_viewer(
        st.session_state["watchlist"],
        selected_detail_wallet,
        ranked_lookup=detail_lookup,
        require_watchlist=True,
    )
    st.stop()

render_section_header(
    "Whale Leaderboard",
    "Ranked Signal",
    "Prioritizes realized edge, capital deployed, sample size, and copyability under the active filters.",
    right_html=f'{badge("Read-only analytics", "green")}{badge("CSV export", "blue")}',
)

score_column = "whale_score" if whale_mode else "final_score"
if score_column not in df.columns:
    df[score_column] = 0
roi_values = pd.to_numeric(df["roi_pct"], errors="coerce").dropna() if not df.empty else pd.Series(dtype=float)
top_cols = st.columns(4)
with top_cols[0]:
    render_metric_card("Wallets analyzed", f"{len(rows):,}", "Public wallets processed", "blue")
with top_cols[1]:
    render_metric_card("Passing filters", f"{len(filtered):,}", "Qualified whale candidates", "green")
with top_cols[2]:
    render_metric_card("Best score", f"{df[score_column].max():.2f}" if not df.empty else "N/A", score_column.replace("_", " ").title(), "green")
with top_cols[3]:
    render_metric_card("Median ROI", f"{roi_values.median():.2f}%" if not roi_values.empty else "N/A", "Filtered wallet set", "blue")

if df.empty:
    render_empty_state("No wallets passed", "Lower the thresholds or scan a larger wallet set to widen the signal pool.")
    st.stop()

if whale_mode:
    columns = [
        "whale_tier",
        "wallet",
        "net_profit",
        "roi_pct",
        "win_rate",
        "adjusted_win_rate",
        "total_volume",
        "avg_trade_size",
        "recent_activity",
        "whale_score",
        "trend_score",
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
df = add_compact_wallet_table_fields(df)
if "whale_tier" not in df.columns:
    df["whale_tier"] = "Dolphin"
if "whale_score" not in df.columns:
    df["whale_score"] = df.get("final_score", 0)
for column in columns:
    if column not in df.columns:
        df[column] = None
visible_columns = [
    "whale_tier",
    "wallet",
    "whale_score",
    "trend_score",
    "net_profit",
    "roi_pct",
    "win_rate",
    "adjusted_win_rate",
    "total_volume",
    "avg_trade_size",
    "recent_activity",
]
for column in visible_columns:
    if column not in df.columns:
        df[column] = 0 if column not in ("whale_tier", "wallet") else ""

available_wallets = {str(wallet).lower() for wallet in df["wallet"].dropna().astype(str)}
st.caption("Select one wallet row to open the compact wallet panel below. The table keeps full wallet addresses and scrolls horizontally when needed.")
ranking_event = st.dataframe(
    style_financial_table(df[visible_columns]),
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    key="wallet_rankings_table",
    column_config={
        "whale_tier": st.column_config.TextColumn("Tier", width=130),
        "wallet": st.column_config.TextColumn(
            "Wallet Address",
            help="Full wallet address. Select a row to copy it from the detail panel.",
            width=420,
            pinned=True,
        ),
        "whale_score": st.column_config.NumberColumn("Whale Score", format="%.2f", width=135, alignment="right"),
        "trend_score": st.column_config.NumberColumn("Trend Score", format="%.2f", width=135, alignment="right"),
        "net_profit": st.column_config.NumberColumn("Net Profit", format="$%.2f", width=145, alignment="right"),
        "roi_pct": st.column_config.NumberColumn("ROI %", format="%.2f%%", width=110, alignment="right"),
        "win_rate": st.column_config.NumberColumn("Win Rate", format="%.2f%%", width=120, alignment="right"),
        "adjusted_win_rate": st.column_config.NumberColumn("Adjusted Win Rate", format="%.2f%%", width=175, alignment="right"),
        "total_volume": st.column_config.NumberColumn("Total Volume", format="$%.2f", width=155, alignment="right"),
        "avg_trade_size": st.column_config.NumberColumn("Average Position Size", format="$%.2f", width=205, alignment="right"),
        "recent_activity": st.column_config.NumberColumn("Recent Trades", format="%d", width=145, alignment="right"),
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
elif str(st.session_state.get("selected_wallet") or "").lower() in available_wallets:
    selected_wallet = str(st.session_state["selected_wallet"])

if selected_wallet:
    selected_row = df[df["wallet"].astype(str) == selected_wallet]
    selected_metadata = selected_row.iloc[0].to_dict() if not selected_row.empty else None
    render_selected_wallet_panel(selected_wallet, selected_metadata)
    if st.session_state.get("show_selected_wallet_trades") == selected_wallet:
        render_wallet_trade_viewer(
            [selected_wallet],
            selected_wallet,
            ranked_lookup={selected_wallet.lower(): selected_metadata or {}},
            require_watchlist=False,
        )

csv_columns = [column for column in columns if column in df.columns]
csv = df[csv_columns].to_csv(index=False).encode("utf-8")
st.download_button("Export results to CSV", csv, "whalewatch_wallet_rankings.csv", "text/csv")

with st.expander("How the score works"):
    st.markdown(
        """
        Whale Mode uses normalized 0-100 inputs so one metric cannot overpower everything:

        `Profit x 0.30 + Win Rate x 0.25 + Volume x 0.20 + Position Sizing x 0.10 + Consistency x 0.10 + Activity Frequency x 0.05`

        Bot-like behavior, repetitive same-size trades, tiny high-frequency flow, and concentrated one-off wins reduce the score.
        Tiers are Kraken, Leviathan, Blue Whale, Shark, and Dolphin.

        Standard mode uses this reliability-adjusted ranking:

        `Adjusted Win Rate x 0.40 + ROI x 0.20 + Copyability x 0.20 + Trade Liquidity Quality x 0.10 + Trade Count Reliability x 0.10`

        Adjusted Win Rate is `(wallet wins + 5) / (wallet resolved markets + 10)`, which prevents tiny samples from ranking first.
        ROI is normalized from profit relative to amount bought. Copyability rewards smoother drawdowns, avoids wallets
        dominated by one huge win, estimates whether entries were followed by favorable 24-hour price movement, and
        mildly penalizes very large average trade sizes. Liquidity quality uses stable trade-data proxies: total volume,
        average trade size, unique markets, recent activity, and resolved market count.
        """
    )
