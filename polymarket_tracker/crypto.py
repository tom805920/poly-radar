from __future__ import annotations

import time
from collections import defaultdict

import requests

from .metrics import normalize_to_100, whale_tier


REQUEST_TIMEOUT_SECONDS = 10
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


CHAIN_CONFIGS = {
    "Ethereum": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "1",
        "explorer": "https://etherscan.io/address/{wallet}",
        "block_time_seconds": 12,
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_usd_estimate": 3000,
        "log_chunk_blocks": 2500,
        "stable_tokens": {
            "USDT": {"contract": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
            "USDC": {"contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
        },
    },
    "BNB Chain": {
        "api_url": "https://api.bscscan.com/api",
        "chainid": None,
        "explorer": "https://bscscan.com/address/{wallet}",
        "block_time_seconds": 3,
        "native_symbol": "BNB",
        "native_decimals": 18,
        "native_usd_estimate": 600,
        "log_chunk_blocks": 5000,
        "stable_tokens": {
            "USDT": {"contract": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18},
            "USDC": {"contract": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18},
        },
    },
    "Base": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "8453",
        "explorer": "https://basescan.org/address/{wallet}",
        "block_time_seconds": 2,
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_usd_estimate": 3000,
        "log_chunk_blocks": 5000,
        "stable_tokens": {
            "USDC": {"contract": "0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913", "decimals": 6},
            "USDbC": {"contract": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
        },
    },
    "Arbitrum": {
        "api_url": "https://api.etherscan.io/v2/api",
        "chainid": "42161",
        "explorer": "https://arbiscan.io/address/{wallet}",
        "block_time_seconds": 1,
        "native_symbol": "ETH",
        "native_decimals": 18,
        "native_usd_estimate": 3000,
        "log_chunk_blocks": 10000,
        "stable_tokens": {
            "USDC": {"contract": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            "USDT": {"contract": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "decimals": 6},
        },
    },
}


class CryptoAPIError(Exception):
    pass


PUBLIC_CEX_RELATED_WALLETS = {
    "Ethereum": [
        # Publicly labelled exchange-related wallet examples. These are not private exchange users.
        "0x28c6c06298d514db089934071355e5743bf21d60",
    ],
    "BNB Chain": [],
    "Base": [],
    "Arbitrum": [],
}


def _request_json(url: str, params: dict, context: str = "Explorer request") -> dict:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                raise CryptoAPIError(f"{context} failed: explorer returned non-JSON data.")
            payload = response.json()
            if response.status_code >= 400:
                raise CryptoAPIError(f"{context} failed with HTTP {response.status_code}: {payload}")
            return payload if isinstance(payload, dict) else {"status": "0", "message": "Bad payload", "result": []}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            last_error = exc
    raise CryptoAPIError(f"{context} failed: {last_error or 'request failed'}")


def _api_params(chain: str, api_key: str | None, module: str = "account", **params) -> tuple[str, dict]:
    config = CHAIN_CONFIGS[chain]
    merged = {"module": module, **params}
    if config.get("chainid"):
        merged["chainid"] = config["chainid"]
    if api_key:
        merged["apikey"] = api_key
    return str(config["api_url"]), merged


def _result_list(payload: dict, context: str, allow_empty: bool = True) -> list:
    result = payload.get("result") if isinstance(payload, dict) else []
    if isinstance(result, list):
        return result
    message = str(payload.get("message") or payload.get("result") or "").lower() if isinstance(payload, dict) else ""
    if allow_empty and any(text in message for text in ["no records", "no transactions", "no matching", "not found"]):
        return []
    raise CryptoAPIError(f"{context} failed: {payload.get('message') or payload.get('result') or 'unexpected response'}")


def _result_hex(payload: dict, context: str) -> str:
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, str) and result.startswith("0x"):
        return result
    raise CryptoAPIError(f"{context} failed: {payload.get('message') or result or 'unexpected response'}")


def _latest_block(chain: str, api_key: str | None) -> int:
    url, params = _api_params(chain, api_key, module="proxy", action="eth_blockNumber")
    payload = _request_json(url, params, context=f"{chain} latest block")
    return int(_result_hex(payload, f"{chain} latest block"), 16)


def _block_window(chain: str, api_key: str | None, time_period_days: int) -> tuple[int, int]:
    latest = _latest_block(chain, api_key)
    config = CHAIN_CONFIGS[chain]
    blocks = int((min(int(time_period_days), 30) * 24 * 3600) / max(int(config["block_time_seconds"]), 1))
    # Explorer log APIs cap large scans. Use recent blocks and chunk them.
    blocks = min(blocks, 120_000)
    return max(0, latest - blocks), latest


def _topic_address(topic: str) -> str:
    topic = str(topic or "").lower()
    if not topic.startswith("0x") or len(topic) < 66:
        return ""
    return "0x" + topic[-40:]


def _hex_int(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value)
    if text.startswith("0x"):
        return int(text, 16)
    return int(text or 0)


def _timestamp_from_log(row: dict) -> int:
    value = row.get("timeStamp") or row.get("timestamp")
    if value is None:
        return int(time.time())
    try:
        return _hex_int(value)
    except (TypeError, ValueError):
        return int(time.time())


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


def _normalize_transfer_log(row: dict, chain: str, symbol: str, decimals: int) -> dict | None:
    topics = row.get("topics") or []
    if not isinstance(topics, list) or len(topics) < 3:
        return None
    from_wallet = _topic_address(str(topics[1]))
    to_wallet = _topic_address(str(topics[2]))
    if not from_wallet or not to_wallet:
        return None
    try:
        raw_value = _hex_int(row.get("data"))
    except (TypeError, ValueError):
        raw_value = 0
    amount = raw_value / (10 ** int(decimals or 18))
    timestamp = _timestamp_from_log(row)
    return {
        "chain": chain,
        "hash": str(row.get("transactionHash") or row.get("hash") or ""),
        "from": from_wallet,
        "to": to_wallet,
        "token": symbol,
        "amount": amount,
        "value_usd": amount if symbol.upper() in {"USDT", "USDC", "USDBC", "DAI"} else 0.0,
        "timestamp_raw": timestamp,
        "timestamp": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp)) if timestamp else "",
        "block_number": _hex_int(row.get("blockNumber")),
        "source": "erc20_log",
    }


def _fetch_large_transfer_logs(
    chain: str,
    api_key: str | None,
    contract: str,
    symbol: str,
    decimals: int,
    min_transaction_size: float,
    time_period_days: int,
    max_logs: int = 1000,
) -> list[dict]:
    start_block, latest_block = _block_window(chain, api_key, int(time_period_days))
    chunk_size = int(CHAIN_CONFIGS[chain].get("log_chunk_blocks") or 5000)
    rows: list[dict] = []
    current_to = latest_block
    while current_to >= start_block and len(rows) < max_logs:
        current_from = max(start_block, current_to - chunk_size + 1)
        url, params = _api_params(
            chain,
            api_key,
            module="logs",
            action="getLogs",
            fromBlock=current_from,
            toBlock=current_to,
            address=contract,
            topic0=TRANSFER_TOPIC,
        )
        payload = _request_json(url, params, context=f"{chain} {symbol} transfer logs")
        for item in _result_list(payload, f"{chain} {symbol} transfer logs"):
            transfer = _normalize_transfer_log(item, chain, symbol, decimals)
            if not transfer:
                continue
            if float(transfer.get("value_usd") or 0) >= float(min_transaction_size):
                rows.append(transfer)
        current_to = current_from - 1
    return sorted(rows, key=lambda row: int(row.get("timestamp_raw") or 0), reverse=True)


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
    result = _result_list(payload, f"{chain} {symbol} token transfers")
    return [_normalize_transfer(row, chain, symbol, decimals) for row in result if isinstance(row, dict)]


def _normalize_native_tx(row: dict, chain: str, wallet: str) -> dict:
    config = CHAIN_CONFIGS[chain]
    decimals = int(config.get("native_decimals") or 18)
    symbol = str(config.get("native_symbol") or "NATIVE")
    native_usd = float(config.get("native_usd_estimate") or 0)
    try:
        amount = float(row.get("value") or 0) / (10 ** decimals)
    except (TypeError, ValueError):
        amount = 0.0
    timestamp = int(row.get("timeStamp") or 0)
    return {
        "chain": chain,
        "hash": str(row.get("hash") or ""),
        "from": str(row.get("from") or "").lower(),
        "to": str(row.get("to") or "").lower(),
        "token": symbol,
        "amount": amount,
        "value_usd": amount * native_usd,
        "timestamp_raw": timestamp,
        "timestamp": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp)) if timestamp else "",
        "source": "native_transfer",
    }


def _fetch_native_transactions(chain: str, api_key: str | None, address: str, offset: int = 100) -> list[dict]:
    url, params = _api_params(
        chain,
        api_key,
        action="txlist",
        address=address,
        page=1,
        offset=offset,
        sort="desc",
    )
    payload = _request_json(url, params, context=f"{chain} native transfers")
    result = _result_list(payload, f"{chain} native transfers")
    return [_normalize_native_tx(row, chain, address) for row in result if isinstance(row, dict)]


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
    if not api_key:
        raise CryptoAPIError(f"{chain} discovery requires an explorer API key in Streamlit secrets.")
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
        transfers = _fetch_large_transfer_logs(
            chain,
            api_key,
            token["contract"],
            symbol,
            int(token["decimals"]),
            min_transaction_size=float(min_transaction_size),
            time_period_days=int(time_period_days),
            max_logs=1000,
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
    for seed_wallet in _seed_wallets_from_text(seed_wallets):
        for transfer in fetch_crypto_wallet_activity(seed_wallet, chain, api_key, token_filter, time_period_days):
            value = float(transfer.get("value_usd") or 0)
            if value < float(min_transaction_size):
                continue
            for side in ("from", "to"):
                wallet = str(transfer.get(side) or "").lower()
                if wallet.startswith("0x") and len(wallet) == 42:
                    wallet_volume[wallet] += value * 0.75
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
    native_transfers = _fetch_native_transactions(chain, api_key, wallet, offset=100)
    transfers.extend(native_transfers)
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
    estimated_net_flow = incoming - outgoing
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
        "net_profit": round(estimated_net_flow, 2),
        "estimated_net_flow": round(estimated_net_flow, 2),
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
        "profit_note": "Realized P/L is not reliably calculable from public transfer data alone; net profit shows estimated net on-chain flow.",
        "explorer_url": CHAIN_CONFIGS[chain]["explorer"].format(wallet=wallet),
    }
