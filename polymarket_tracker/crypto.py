from __future__ import annotations

import math
import time
from collections import Counter, defaultdict

import requests

from .metrics import normalize_to_100, whale_tier


REQUEST_TIMEOUT_SECONDS = 10


CHAIN_CONFIGS = {
    "Ethereum": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "1",
        "explorer": "https://etherscan.io/address/{wallet}",
        "stable_tokens": {
            "USDT": {"contract": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
            "USDC": {"contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
        },
    },
    "BNB Chain": {
        "api_url": "https://api.bscscan.com/api",
        "chainid": None,
        "explorer": "https://bscscan.com/address/{wallet}",
        "stable_tokens": {
            "USDT": {"contract": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18},
            "USDC": {"contract": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18},
        },
    },
    "Base": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "8453",
        "explorer": "https://basescan.org/address/{wallet}",
        "stable_tokens": {
            "USDC": {"contract": "0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913", "decimals": 6},
            "USDbC": {"contract": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
        },
    },
    "Arbitrum": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "42161",
        "explorer": "https://arbiscan.io/address/{wallet}",
        "stable_tokens": {
            "USDC": {"contract": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            "USDT": {"contract": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "decimals": 6},
        },
    },
}


PUBLIC_CEX_RELATED_WALLETS = {
    "Ethereum": [
        # Publicly labelled exchange-related wallet examples. These are not private exchange users.
        "0x28c6c06298d514db089934071355e5743bf21d60",
    ],
    "BNB Chain": [],
    "Base": [],
    "Arbitrum": [],
}


def _request_json(url: str, params: dict) -> dict:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                return {"status": "0", "message": "Non-JSON response", "result": []}
            payload = response.json()
            if response.status_code >= 400:
                return {"status": "0", "message": str(payload), "result": []}
            return payload if isinstance(payload, dict) else {"status": "0", "message": "Bad payload", "result": []}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            last_error = exc
    return {"status": "0", "message": str(last_error or "Request failed"), "result": []}


def _api_params(chain: str, api_key: str | None, **params) -> tuple[str, dict]:
    config = CHAIN_CONFIGS[chain]
    merged = {"module": "account", **params}
    if config.get("chainid"):
        merged["chainid"] = config["chainid"]
    if api_key:
        merged["apikey"] = api_key
    return str(config["api_url"]), merged


def _token_transfer_value(row: dict, fallback_decimals: int) -> float:
    decimals = int(row.get("tokenDecimal") or fallback_decimals or 18)
    try:
        raw_value = float(row.get("value") or 0)
    except (TypeError, ValueError):
        raw_value = 0.0
    return raw_value / (10 ** decimals)


def _normalize_transfer(row: dict, chain: str, fallback_symbol: str, fallback_decimals: int) -> dict:
    amount = _token_transfer_value(row, fallback_decimals)
    symbol = str(row.get("tokenSymbol") or fallback_symbol or "TOKEN")
    timestamp = int(row.get("timeStamp") or 0)
    from_wallet = str(row.get("from") or "").lower()
    to_wallet = str(row.get("to") or "").lower()
    return {
        "chain": chain,
        "hash": str(row.get("hash") or ""),
        "from": from_wallet,
        "to": to_wallet,
        "token": symbol,
        "amount": amount,
        "value_usd": amount if symbol.upper() in {"USDT", "USDC", "USDBC", "DAI"} else 0.0,
        "timestamp_raw": timestamp,
        "timestamp": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp)) if timestamp else "",
    }


def _fetch_token_transfers(
    chain: str,
    api_key: str | None,
    contract: str,
    symbol: str,
    decimals: int,
    address: str | None = None,
    offset: int = 200,
) -> list[dict]:
    params = {
        "action": "tokentx",
        "contractaddress": contract,
        "page": 1,
        "offset": offset,
        "sort": "desc",
    }
    if address:
        params["address"] = address
    url, merged = _api_params(chain, api_key, **params)
    payload = _request_json(url, merged)
    result = payload.get("result") if isinstance(payload, dict) else []
    if not isinstance(result, list):
        return []
    return [_normalize_transfer(row, chain, symbol, decimals) for row in result if isinstance(row, dict)]


def _seed_wallets_from_text(seed_wallets: list[str] | None) -> list[str]:
    wallets = []
    for wallet in seed_wallets or []:
        text = str(wallet).strip().lower()
        if text.startswith("0x") and len(text) == 42:
            wallets.append(text)
    return list(dict.fromkeys(wallets))


def discover_crypto_whales(
    chain: str,
    api_key: str | None = None,
    token_filter: str = "All",
    min_transaction_size: float = 25000,
    max_wallets: int = 50,
    time_period_days: int = 30,
    include_cex_related: bool = False,
    seed_wallets: list[str] | None = None,
) -> list[str]:
    """Discover public on-chain candidate wallets from large stable-token transfers.

    This intentionally does not attempt to infer private CEX user accounts.
    """
    if chain not in CHAIN_CONFIGS:
        return []
    since_ts = int(time.time()) - int(time_period_days) * 24 * 3600
    config = CHAIN_CONFIGS[chain]
    wallet_volume: dict[str, float] = defaultdict(float)
    for wallet in _seed_wallets_from_text(seed_wallets):
        wallet_volume[wallet] += float(min_transaction_size)
    if include_cex_related:
        for wallet in PUBLIC_CEX_RELATED_WALLETS.get(chain, []):
            wallet_volume[wallet.lower()] += float(min_transaction_size)
    token_items = config["stable_tokens"].items()
    if token_filter and token_filter != "All":
        token_items = [(token_filter, config["stable_tokens"][token_filter])] if token_filter in config["stable_tokens"] else []
    for symbol, token in token_items:
        transfers = _fetch_token_transfers(
            chain,
            api_key,
            token["contract"],
            symbol,
            int(token["decimals"]),
            address=None,
            offset=200,
        )
        for transfer in transfers:
            if int(transfer.get("timestamp_raw") or 0) < since_ts:
                continue
            value = float(transfer.get("value_usd") or 0)
            if value < float(min_transaction_size):
                continue
            for side in ("from", "to"):
                wallet = str(transfer.get(side) or "").lower()
                if wallet.startswith("0x") and len(wallet) == 42:
                    wallet_volume[wallet] += value
    return [
        wallet
        for wallet, _volume in sorted(wallet_volume.items(), key=lambda item: item[1], reverse=True)[: int(max_wallets)]
    ]


def fetch_crypto_wallet_activity(
    wallet: str,
    chain: str,
    api_key: str | None = None,
    token_filter: str = "All",
    time_period_days: int = 30,
) -> list[dict]:
    if chain not in CHAIN_CONFIGS:
        return []
    wallet = str(wallet).lower()
    since_ts = int(time.time()) - int(time_period_days) * 24 * 3600
    config = CHAIN_CONFIGS[chain]
    token_items = config["stable_tokens"].items()
    if token_filter and token_filter != "All":
        token_items = [(token_filter, config["stable_tokens"][token_filter])] if token_filter in config["stable_tokens"] else []
    transfers: list[dict] = []
    for symbol, token in token_items:
        transfers.extend(
            _fetch_token_transfers(
                chain,
                api_key,
                token["contract"],
                symbol,
                int(token["decimals"]),
                address=wallet,
                offset=200,
            )
        )
    filtered = [row for row in transfers if int(row.get("timestamp_raw") or 0) >= since_ts]
    return sorted(filtered, key=lambda row: int(row.get("timestamp_raw") or 0), reverse=True)


def fetch_crypto_recent_trades(
    wallet: str,
    chain: str,
    api_key: str | None = None,
    token_filter: str = "All",
    time_period_days: int = 30,
) -> list[dict]:
    return fetch_crypto_wallet_activity(wallet, chain, api_key, token_filter, time_period_days)


def fetch_crypto_token_balances(wallet: str, chain: str, api_key: str | None = None) -> list[dict]:
    # Placeholder adapter: balances vary heavily by provider. Kept separate so a richer provider
    # can be swapped in without touching the UI/scoring code.
    return []


def calculate_crypto_wallet_score(
    wallet: str,
    activity: list[dict],
    chain: str,
    min_transaction_size: float = 25000,
) -> dict:
    wallet = str(wallet).lower()
    values = [float(row.get("value_usd") or 0) for row in activity if float(row.get("value_usd") or 0) > 0]
    total_volume = sum(values)
    trade_count = len(values)
    avg_trade_size = total_volume / trade_count if trade_count else 0.0
    largest_trade = max(values) if values else 0.0
    recent_activity = sum(1 for row in activity if int(row.get("timestamp_raw") or 0) >= int(time.time()) - 7 * 24 * 3600)
    tokens = {str(row.get("token") or "") for row in activity if row.get("token")}
    incoming = sum(float(row.get("value_usd") or 0) for row in activity if str(row.get("to") or "").lower() == wallet)
    outgoing = sum(float(row.get("value_usd") or 0) for row in activity if str(row.get("from") or "").lower() == wallet)
    directional_balance = 100 - min(100, abs(incoming - outgoing) / max(total_volume, 1) * 100)
    size_reliability = normalize_to_100(avg_trade_size, max(float(min_transaction_size) * 2, 1))
    volume_score = normalize_to_100(total_volume, 1_000_000)
    activity_score = normalize_to_100(recent_activity, 20)
    frequency_score = normalize_to_100(trade_count, 80)
    diversity_score = normalize_to_100(len(tokens), 4)
    concentration_penalty = normalize_to_100(largest_trade / max(total_volume, 1) * 100, 85) * 0.18 if trade_count > 1 else 12
    whale_score = (
        volume_score * 0.35
        + size_reliability * 0.25
        + activity_score * 0.15
        + directional_balance * 0.10
        + diversity_score * 0.10
        + frequency_score * 0.05
        - concentration_penalty
    )
    whale_score = round(max(0, min(100, whale_score)), 2)
    return {
        "wallet": wallet,
        "chain": chain,
        "whale_tier": whale_tier(whale_score),
        "whale_score": whale_score,
        "trend_score": round(min(100, activity_score * 0.6 + volume_score * 0.4), 2),
        "net_profit": 0.0,
        "roi_pct": 0.0,
        "win_rate": 0.0,
        "adjusted_win_rate": 0.0,
        "total_volume": round(total_volume, 2),
        "avg_trade_size": round(avg_trade_size, 2),
        "largest_trade": round(largest_trade, 2),
        "recent_activity": int(recent_activity),
        "trade_count": int(trade_count),
        "unique_tokens": int(len(tokens)),
        "consistency_score": round(directional_balance, 2),
        "profit_note": "Realized P/L and win rate are not reliably calculable from public transfer data alone.",
        "explorer_url": CHAIN_CONFIGS[chain]["explorer"].format(wallet=wallet),
    }
