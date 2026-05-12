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


QUOTE_TOKENS = {"ETH", "WETH", "BNB", "WBNB", "USDT", "USDC", "USDBC", "DAI"}


DEX_ROUTER_CONTRACTS = {
    "Ethereum": {
        "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
        "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
        "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3",
        "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uniswap Universal Router",
        "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "SushiSwap",
        "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch",
        "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch",
        "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer",
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x",
    },
    "BNB Chain": {
        "0x10ed43c718714eb63d5aa57b78b54704e256024e": "PancakeSwap V2",
        "0x13f4ea83d0bd40e75c8222255bc855a974568dd4": "PancakeSwap V3",
        "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch",
        "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch",
        "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506": "SushiSwap",
    },
    "Base": {
        "0x3fC91A3afd70395CD496C647d5a6CC9D4B2b7FAD": "Uniswap Universal Router",
        "0x2626664c2603336E57B271c5C0b26F421741e481": "Uniswap V3",
        "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch",
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x",
    },
    "Arbitrum": {
        "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3",
        "0x5e325eda8064b456f4781070c0738d849c824258": "Uniswap Universal Router",
        "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch",
        "0x1b02da8cb0d097eb8d57a175b88c7d8b47997506": "SushiSwap",
        "0xba12222222228d8ba445958a75a0704d566bf2c8": "Balancer",
    },
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
        "source": "erc20_transfer",
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
        "page": 1,
        "offset": offset,
        "sort": "desc",
    }
    if contract:
        params["contractaddress"] = contract
    if address:
        params["address"] = address
    url, merged = _api_params(chain, api_key, **params)
    payload = _request_json(url, merged)
    result = _result_list(payload, f"{chain} {symbol} token transfers")
    return [_normalize_transfer(row, chain, symbol, decimals) for row in result if isinstance(row, dict)]


def _fetch_wallet_token_transfers(chain: str, api_key: str | None, address: str, offset: int = 500) -> list[dict]:
    url, params = _api_params(
        chain,
        api_key,
        action="tokentx",
        address=address,
        page=1,
        offset=offset,
        sort="desc",
    )
    payload = _request_json(url, params, context=f"{chain} token transfers")
    result = _result_list(payload, f"{chain} token transfers")
    return [_normalize_transfer(row, chain, "", 18) for row in result if isinstance(row, dict)]


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
        "method_id": str(row.get("methodId") or ""),
        "function_name": str(row.get("functionName") or ""),
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
    transfers: list[dict] = []
    token_transfers = _fetch_wallet_token_transfers(chain, api_key, wallet, offset=500)
    if token_filter and token_filter != "All":
        token_transfers = [
            row for row in token_transfers
            if str(row.get("token") or "").upper() == str(token_filter).upper()
        ]
    transfers.extend(token_transfers)
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


def _explorer_tx_url(chain: str, tx_hash: str) -> str:
    base = CHAIN_CONFIGS[chain]["explorer"].split("/address/")[0]
    return f"{base}/tx/{tx_hash}" if tx_hash else ""


def _is_router(chain: str, address: str) -> bool:
    routers = {key.lower(): value for key, value in DEX_ROUTER_CONTRACTS.get(chain, {}).items()}
    return str(address or "").lower() in routers


def _router_name(chain: str, address: str) -> str:
    routers = {key.lower(): value for key, value in DEX_ROUTER_CONTRACTS.get(chain, {}).items()}
    return routers.get(str(address or "").lower(), "")


def _is_public_cex_wallet(chain: str, address: str) -> bool:
    address = str(address or "").lower()
    return address in {wallet.lower() for wallet in PUBLIC_CEX_RELATED_WALLETS.get(chain, [])}


def _asset_value_usd(asset: dict) -> float:
    symbol = str(asset.get("token") or "").upper()
    amount = float(asset.get("amount") or 0)
    if symbol in {"USDT", "USDC", "USDBC", "DAI"}:
        return amount
    return float(asset.get("value_usd") or 0)


def _best_asset(assets: list[dict]) -> dict:
    if not assets:
        return {}
    return sorted(
        assets,
        key=lambda row: (_asset_value_usd(row), float(row.get("amount") or 0)),
        reverse=True,
    )[0]


def _counterparty(row: dict, wallet: str) -> str:
    from_wallet = str(row.get("from") or "").lower()
    to_wallet = str(row.get("to") or "").lower()
    if from_wallet == wallet:
        return to_wallet
    if to_wallet == wallet:
        return from_wallet
    return to_wallet or from_wallet


def _tx_router_context(chain: str, rows: list[dict], wallet: str) -> tuple[bool, str]:
    for row in rows:
        for address in [row.get("from"), row.get("to"), _counterparty(row, wallet)]:
            if _is_router(chain, str(address or "")):
                return True, _router_name(chain, str(address or ""))
        function_name = str(row.get("function_name") or "").lower()
        if any(word in function_name for word in ["swap", "multicall", "exactinput", "unoswap", "uniswap"]):
            return True, "DEX router"
    return False, ""


def _action_for_swap(sold_symbol: str, bought_symbol: str, confirmed: bool) -> tuple[str, str]:
    sold_quote = sold_symbol.upper() in QUOTE_TOKENS
    bought_quote = bought_symbol.upper() in QUOTE_TOKENS
    prefix = "" if confirmed else "Possible swap: "
    if sold_quote and not bought_quote:
        return "BUY", f"{prefix}Bought {bought_symbol} with {sold_symbol}"
    if bought_quote and not sold_quote:
        return "SELL", f"{prefix}Sold {sold_symbol} for {bought_symbol}"
    return "SWAP", f"{prefix}Swapped {sold_symbol} -> {bought_symbol}"


def build_crypto_activity_events(wallet: str, transfers: list[dict], chain: str) -> list[dict]:
    wallet = str(wallet).lower()
    groups: dict[str, list[dict]] = defaultdict(list)
    for transfer in transfers:
        tx_hash = str(transfer.get("hash") or "")
        if tx_hash:
            groups[tx_hash].append(transfer)

    events = []
    for tx_hash, rows in groups.items():
        outgoing = [
            row for row in rows
            if str(row.get("from") or "").lower() == wallet and float(row.get("amount") or 0) > 0
        ]
        incoming = [
            row for row in rows
            if str(row.get("to") or "").lower() == wallet and float(row.get("amount") or 0) > 0
        ]
        timestamp_raw = max(int(row.get("timestamp_raw") or 0) for row in rows)
        timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(timestamp_raw)) if timestamp_raw else ""
        router_involved, router_name = _tx_router_context(chain, rows, wallet)

        if incoming and outgoing:
            sold = _best_asset(outgoing)
            bought = _best_asset(incoming)
            action, description = _action_for_swap(
                str(sold.get("token") or "UNKNOWN"),
                str(bought.get("token") or "UNKNOWN"),
                router_involved,
            )
            estimated_value = max(_asset_value_usd(sold), _asset_value_usd(bought))
            events.append(
                {
                    "wallet": wallet,
                    "chain": chain,
                    "timestamp_raw": timestamp_raw,
                    "timestamp": timestamp,
                    "category": "Trades / Swaps",
                    "action": action,
                    "description": description,
                    "token_sold": sold.get("token") or "",
                    "token_bought": bought.get("token") or "",
                    "amount_sold": sold.get("amount") or 0,
                    "amount_bought": bought.get("amount") or 0,
                    "dollar_value": round(estimated_value, 2),
                    "tx_hash": tx_hash,
                    "explorer_link": _explorer_tx_url(chain, tx_hash),
                    "confidence": "Confirmed swap" if router_involved else "Possible swap",
                    "venue": router_name or "Unknown DEX",
                }
            )
            continue

        transfer = _best_asset(incoming or outgoing or rows)
        direction = "Received" if incoming else "Sent" if outgoing else "Transfer"
        token = str(transfer.get("token") or "")
        amount = float(transfer.get("amount") or 0)
        estimated_value = _asset_value_usd(transfer)
        counterparty = _counterparty(transfer, wallet)
        if estimated_value >= 10_000:
            category = "Large Transfers"
        elif _is_public_cex_wallet(chain, counterparty):
            category = "Deposits / Withdrawals"
        else:
            category = "Unknown Activity"
        events.append(
            {
                "wallet": wallet,
                "chain": chain,
                "timestamp_raw": timestamp_raw,
                "timestamp": timestamp,
                "category": category,
                "action": "RECEIVE" if direction == "Received" else "SEND" if direction == "Sent" else "TRANSFER",
                "description": f"{direction} {token}",
                "token_sold": token if direction == "Sent" else "",
                "token_bought": token if direction == "Received" else "",
                "amount_sold": amount if direction == "Sent" else 0,
                "amount_bought": amount if direction == "Received" else 0,
                "dollar_value": round(estimated_value, 2),
                "tx_hash": tx_hash,
                "explorer_link": _explorer_tx_url(chain, tx_hash),
                "confidence": "Transfer",
                "venue": "",
            }
        )

    category_rank = {"Trades / Swaps": 0, "Large Transfers": 1, "Deposits / Withdrawals": 2, "Unknown Activity": 3}
    return sorted(
        events,
        key=lambda row: (category_rank.get(str(row.get("category")), 9), -int(row.get("timestamp_raw") or 0)),
    )


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
