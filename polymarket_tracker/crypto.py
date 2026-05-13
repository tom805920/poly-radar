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


SYSTEM_WALLETS = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x000000000000000000000000000000000000dEaD".lower(),
}


QUOTE_TOKENS = {"ETH", "WETH", "BNB", "WBNB", "USDT", "USDC", "USDBC", "DAI"}


def _clamp(value: float, floor: float = 0.0, ceiling: float = 100.0) -> float:
    return max(floor, min(ceiling, float(value)))


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


def _known_contract_addresses(chain: str) -> set[str]:
    routers = {address.lower() for address in DEX_ROUTER_CONTRACTS.get(chain, {})}
    stable_tokens = {
        str(token.get("contract") or "").lower()
        for token in CHAIN_CONFIGS.get(chain, {}).get("stable_tokens", {}).values()
        if token.get("contract")
    }
    return routers | stable_tokens


def _is_excluded_crypto_wallet(chain: str, wallet: str) -> bool:
    wallet = str(wallet or "").lower()
    if not wallet.startswith("0x") or len(wallet) != 42:
        return True
    if wallet in SYSTEM_WALLETS:
        return True
    if _is_public_cex_wallet(chain, wallet):
        return True
    if wallet in _known_contract_addresses(chain):
        return True
    return False


def _is_contract_address(chain: str, api_key: str | None, wallet: str) -> bool:
    if not api_key:
        return False
    try:
        url, params = _api_params(
            chain,
            api_key,
            module="proxy",
            action="eth_getCode",
            address=str(wallet).lower(),
            tag="latest",
        )
        payload = _request_json(url, params, context=f"{chain} contract check")
        code = _result_hex(payload, f"{chain} contract check")
        return code not in {"0x", "0x0", ""}
    except CryptoAPIError:
        return False


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
        if not _is_excluded_crypto_wallet(chain, wallet):
            wallet_volume[wallet] += float(min_transaction_size)
    if include_cex_related:
        for wallet in PUBLIC_CEX_RELATED_WALLETS.get(chain, []):
            # Public CEX wallets can be inspected manually, but they are not ranked as copyable traders.
            continue
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
                if wallet.startswith("0x") and len(wallet) == 42 and not _is_excluded_crypto_wallet(chain, wallet):
                    wallet_volume[wallet] += value
    for seed_wallet in _seed_wallets_from_text(seed_wallets):
        for transfer in fetch_crypto_wallet_activity(seed_wallet, chain, api_key, token_filter, time_period_days):
            value = float(transfer.get("value_usd") or 0)
            if value < float(min_transaction_size):
                continue
            for side in ("from", "to"):
                wallet = str(transfer.get(side) or "").lower()
                if wallet.startswith("0x") and len(wallet) == 42 and not _is_excluded_crypto_wallet(chain, wallet):
                    wallet_volume[wallet] += value * 0.75
    discovered: list[str] = []
    for wallet, _volume in sorted(wallet_volume.items(), key=lambda item: item[1], reverse=True)[: int(max_wallets) * 3]:
        if _is_contract_address(chain, api_key, wallet):
            continue
        discovered.append(wallet)
        if len(discovered) >= int(max_wallets):
            break
    return discovered


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


def _is_quote_token(symbol: str) -> bool:
    return str(symbol or "").upper() in QUOTE_TOKENS


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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


def _quality_reasons(
    profitable_trade_pct: float | None,
    roi_pct: float,
    realized_profit: float,
    avg_trade_size: float,
    trade_count: int,
    consistency_score: float,
    confidence: str,
    completed_trades: int,
    min_transaction_size: float,
) -> list[str]:
    reasons: list[str] = []
    if completed_trades and profitable_trade_pct is not None:
        reasons.append(f"{profitable_trade_pct:.0f}% profitable trades")
    if roi_pct >= 20:
        reasons.append("Strong estimated ROI")
    elif roi_pct < -5:
        reasons.append("Negative estimated ROI")
    if realized_profit > 0:
        reasons.append(f"${realized_profit:,.0f} realized profit estimate")
    if avg_trade_size >= max(float(min_transaction_size), 1) and trade_count >= 3:
        reasons.append("Large repeat trades")
    if consistency_score >= 65 and completed_trades >= 2:
        reasons.append("Consistent trade cycles")
    if confidence != "High":
        reasons.append(f"{confidence} confidence: limited swap-cycle history")
    return reasons[:5] or ["Low confidence: mostly transfers, weak estimate"]


def _copy_quality(
    confidence: str,
    completed_trades: int,
    profitable_trade_pct: float,
    roi_pct: float,
    consistency_score: float,
    max_drawdown_pct: float,
    whale_score: float,
) -> str:
    if confidence == "Low" or completed_trades == 0:
        return "Unproven"
    if profitable_trade_pct < 45 or roi_pct < -5 or max_drawdown_pct >= 45:
        return "Risky"
    if (
        confidence == "High"
        and completed_trades >= 3
        and profitable_trade_pct >= 65
        and roi_pct >= 20
        and consistency_score >= 60
        and whale_score >= 65
    ):
        return "Elite"
    if completed_trades >= 2 and profitable_trade_pct >= 55 and roi_pct >= 0 and consistency_score >= 45:
        return "Strong"
    return "Risky" if roi_pct < 0 else "Unproven"


def _analyze_swap_trade_cycles(
    wallet: str,
    activity: list[dict],
    chain: str,
    min_transaction_size: float,
) -> dict:
    events = build_crypto_activity_events(wallet, activity, chain)
    swap_events = [
        event
        for event in events
        if event.get("category") == "Trades / Swaps" and float(event.get("dollar_value") or 0) > 0
    ]
    lots: dict[str, list[dict]] = defaultdict(list)
    last_prices: dict[str, float] = {}
    completed: list[dict] = []
    realized_profit = 0.0
    completed_cost_basis = 0.0
    open_cost_basis = 0.0
    clear_buy_sell_events = 0
    partial_events = 0

    for event in sorted(swap_events, key=lambda row: int(row.get("timestamp_raw") or 0)):
        action = str(event.get("action") or "").upper()
        timestamp_raw = int(event.get("timestamp_raw") or 0)
        dollar_value = float(event.get("dollar_value") or 0)
        sold_token = str(event.get("token_sold") or "").upper()
        bought_token = str(event.get("token_bought") or "").upper()
        amount_sold = float(event.get("amount_sold") or 0)
        amount_bought = float(event.get("amount_bought") or 0)

        is_clear_buy = action == "BUY" and bought_token and not _is_quote_token(bought_token)
        is_clear_sell = action == "SELL" and sold_token and not _is_quote_token(sold_token)

        if is_clear_buy and amount_bought > 0 and dollar_value > 0:
            lots[bought_token].append(
                {
                    "amount": amount_bought,
                    "cost": dollar_value,
                    "entry_ts": timestamp_raw,
                    "entry_value": dollar_value,
                    "entry_amount": amount_bought,
                    "entry_tx_hash": str(event.get("tx_hash") or ""),
                }
            )
            last_prices[bought_token] = dollar_value / amount_bought
            open_cost_basis += dollar_value
            clear_buy_sell_events += 1
            continue

        if is_clear_sell and amount_sold > 0 and dollar_value > 0:
            last_prices[sold_token] = dollar_value / amount_sold
            remaining_to_match = amount_sold
            matched_amount = 0.0
            matched_cost = 0.0
            weighted_entry = 0.0
            for lot in lots.get(sold_token, []):
                lot_amount = float(lot.get("amount") or 0)
                lot_cost = float(lot.get("cost") or 0)
                if lot_amount <= 0 or lot_cost <= 0 or remaining_to_match <= 0:
                    continue
                take_amount = min(lot_amount, remaining_to_match)
                unit_cost = lot_cost / lot_amount
                take_cost = take_amount * unit_cost
                lot["amount"] = lot_amount - take_amount
                lot["cost"] = lot_cost - take_cost
                remaining_to_match -= take_amount
                matched_amount += take_amount
                matched_cost += take_cost
                weighted_entry += float(lot.get("entry_ts") or timestamp_raw) * take_cost
            if matched_cost > 0 and matched_amount > 0:
                matched_proceeds = dollar_value * (matched_amount / amount_sold)
                profit = matched_proceeds - matched_cost
                realized_profit += profit
                completed_cost_basis += matched_cost
                open_cost_basis = max(0.0, open_cost_basis - matched_cost)
                entry_ts = weighted_entry / matched_cost if matched_cost else timestamp_raw
                completed.append(
                    {
                        "token": sold_token,
                        "entry_value": matched_cost,
                        "exit_value": matched_proceeds,
                        "profit": profit,
                        "cost_basis": matched_cost,
                        "proceeds": matched_proceeds,
                        "return_pct": (profit / matched_cost * 100) if matched_cost else 0.0,
                        "roi_pct": (profit / matched_cost * 100) if matched_cost else 0.0,
                        "hold_hours": max(0.0, (timestamp_raw - entry_ts) / 3600),
                        "entry_timestamp_raw": int(entry_ts),
                        "exit_timestamp_raw": timestamp_raw,
                        "timestamp_raw": timestamp_raw,
                        "entry_tx_hash": next(
                            (
                                str(lot.get("entry_tx_hash") or "")
                                for lot in lots.get(sold_token, [])
                                if lot.get("entry_tx_hash")
                            ),
                            "",
                        ),
                        "exit_tx_hash": str(event.get("tx_hash") or ""),
                        "confidence": "High",
                    }
                )
                clear_buy_sell_events += 1
            else:
                partial_events += 1
            continue

        partial_events += 1

    unrealized_profit = 0.0
    remaining_cost_basis = 0.0
    open_positions = 0
    for token, token_lots in lots.items():
        price = last_prices.get(token)
        for lot in token_lots:
            amount = float(lot.get("amount") or 0)
            cost = float(lot.get("cost") or 0)
            if amount <= 0 or cost <= 0:
                continue
            open_positions += 1
            remaining_cost_basis += cost
            if price is not None and price > 0:
                unrealized_profit += amount * price - cost

    profitable_swaps = sum(1 for item in completed if float(item.get("profit") or 0) > 0)
    losing_swaps = sum(1 for item in completed if float(item.get("profit") or 0) <= 0)
    completed_trades = len(completed)
    profitable_trade_pct = profitable_swaps / completed_trades * 100 if completed_trades else None
    trade_returns = [float(item.get("return_pct") or 0) for item in completed]
    avg_return_per_trade = _mean(trade_returns)
    avg_profit_per_completed_trade = realized_profit / completed_trades if completed_trades else None
    avg_hold_time_hours = _mean([float(item.get("hold_hours") or 0) for item in completed])
    estimated_profit = realized_profit + unrealized_profit if completed_trades else None
    estimated_cost_basis = completed_cost_basis + (remaining_cost_basis if completed_trades else 0.0)
    roi_pct = estimated_profit / estimated_cost_basis * 100 if estimated_profit is not None and estimated_cost_basis else None
    recent_profit_cutoff = int(time.time()) - 7 * 24 * 3600
    recent_profitable_trades = sum(
        1
        for item in completed
        if float(item.get("profit") or 0) > 0 and int(item.get("timestamp_raw") or 0) >= recent_profit_cutoff
    )

    cumulative_profit = 0.0
    peak_profit = 0.0
    max_drawdown = 0.0
    for item in completed:
        cumulative_profit += float(item.get("profit") or 0)
        peak_profit = max(peak_profit, cumulative_profit)
        max_drawdown = max(max_drawdown, peak_profit - cumulative_profit)
    max_drawdown_pct = max_drawdown / max(completed_cost_basis, 1) * 100 if completed else 0.0

    sample_score = normalize_to_100(completed_trades, 12)
    drawdown_resilience = 100 - normalize_to_100(max_drawdown_pct, 50)
    if trade_returns:
        mean_return = _mean(trade_returns)
        dispersion = _mean([abs(value - mean_return) for value in trade_returns])
        return_stability = 100 - normalize_to_100(dispersion, 75)
    else:
        return_stability = 0.0
    profitable_pct_score = profitable_trade_pct or 0.0
    consistency_score = _clamp(
        profitable_pct_score * 0.45 + sample_score * 0.25 + drawdown_resilience * 0.20 + return_stability * 0.10
    ) if completed_trades else 0.0

    if completed_trades >= 1:
        confidence = "High"
        confidence_note = "High: clear buy/sell cycle detected"
    elif swap_events and clear_buy_sell_events:
        confidence = "Medium"
        confidence_note = "Medium: partial swap history"
    else:
        confidence = "Low"
        confidence_note = "Low: mostly transfers, weak estimate"

    trade_values = [float(event.get("dollar_value") or 0) for event in swap_events]
    transfer_values = [float(row.get("value_usd") or 0) for row in activity if float(row.get("value_usd") or 0) > 0]
    total_volume = sum(trade_values) if trade_values else sum(transfer_values)
    trade_count = len(trade_values) if trade_values else len(transfer_values)
    avg_trade_size = total_volume / trade_count if trade_count else 0.0
    completed_timestamps = [int(item.get("timestamp_raw") or 0) for item in completed if int(item.get("timestamp_raw") or 0) > 0]
    if completed_timestamps:
        active_days = max(1, (max(completed_timestamps) - min(completed_timestamps)) / 86400)
        trading_frequency = completed_trades / active_days
    else:
        trading_frequency = 0.0
    trading_frequency_score = normalize_to_100(trading_frequency, 1.0)

    reasons = _quality_reasons(
        profitable_trade_pct,
        roi_pct or 0.0,
        realized_profit,
        avg_trade_size,
        trade_count,
        consistency_score,
        confidence,
        completed_trades,
        min_transaction_size,
    )

    return {
        "events": events,
        "swap_events": swap_events,
        "total_volume": total_volume,
        "trade_count": trade_count,
        "avg_trade_size": avg_trade_size,
        "largest_trade": max(trade_values or transfer_values or [0.0]),
        "completed_trades": completed_trades,
        "completed_cycles": completed,
        "profitable_trade_pct": profitable_trade_pct,
        "profitable_swap_pct": profitable_trade_pct,
        "win_rate": profitable_trade_pct,
        "adjusted_win_rate": (profitable_swaps + 5) / (completed_trades + 10) * 100 if completed_trades else None,
        "roi_pct": roi_pct,
        "avg_return_per_trade": avg_return_per_trade,
        "avg_profit_per_completed_trade": avg_profit_per_completed_trade,
        "profitable_swaps_count": profitable_swaps,
        "losing_swaps_count": losing_swaps,
        "recent_profitable_trades": recent_profitable_trades,
        "trading_frequency": trading_frequency,
        "trading_frequency_score": trading_frequency_score,
        "avg_hold_time_hours": avg_hold_time_hours,
        "realized_profit_estimate": realized_profit,
        "unrealized_profit_estimate": unrealized_profit,
        "estimated_profit": estimated_profit,
        "estimated_cost_basis": estimated_cost_basis,
        "max_drawdown_estimate": max_drawdown_pct,
        "consistency_score": consistency_score,
        "confidence": confidence,
        "confidence_note": confidence_note,
        "copy_reasons": reasons,
        "copy_reason_text": "; ".join(reasons),
        "open_positions": open_positions,
        "partial_trade_events": partial_events,
    }


def calculate_crypto_wallet_score(
    wallet: str,
    activity: list[dict],
    chain: str,
    min_transaction_size: float = 25000,
) -> dict:
    wallet = str(wallet).lower()
    if _is_excluded_crypto_wallet(chain, wallet):
        return {
            "wallet": wallet,
            "chain": chain,
            "whale_tier": "Dolphin",
            "whale_score": 0.0,
            "trend_score": 0.0,
            "net_profit": None,
            "estimated_net_flow": 0.0,
            "roi_pct": None,
            "estimated_roi_pct": None,
            "win_rate": None,
            "profitable_trade_pct": None,
            "profitable_swap_pct": None,
            "adjusted_win_rate": None,
            "total_volume": 0.0,
            "avg_trade_size": 0.0,
            "largest_trade": 0.0,
            "recent_activity": 0,
            "trade_count": 0,
            "unique_tokens": 0,
            "completed_trades": 0,
            "completed_cycles": [],
            "profitable_swaps_count": 0,
            "losing_swaps_count": 0,
            "avg_return_per_trade": None,
            "avg_profit_per_completed_trade": None,
            "recent_profitable_trades": 0,
            "trading_frequency": 0.0,
            "trading_frequency_score": 0.0,
            "avg_hold_time_hours": None,
            "realized_profit_estimate": None,
            "unrealized_profit_estimate": None,
            "max_drawdown_estimate": None,
            "consistency_score": 0.0,
            "confidence": "Low",
            "confidence_level": "Low",
            "confidence_note": "Low: obvious non-trader wallet excluded",
            "copy_quality": "Unproven",
            "copy_reasons": ["Excluded non-trader wallet"],
            "copy_reason_text": "Excluded non-trader wallet",
            "profit_note": "Excluded from crypto scoring because this is an obvious non-trader address.",
            "explorer_url": CHAIN_CONFIGS[chain]["explorer"].format(wallet=wallet),
        }
    quality = _analyze_swap_trade_cycles(wallet, activity, chain, min_transaction_size)
    events = quality["events"]
    total_volume = float(quality["total_volume"])
    trade_count = int(quality["trade_count"])
    avg_trade_size = float(quality["avg_trade_size"])
    largest_trade = float(quality["largest_trade"])
    recent_activity = sum(
        1 for row in events
        if int(row.get("timestamp_raw") or 0) >= int(time.time()) - 7 * 24 * 3600
    )
    if not events:
        recent_activity = sum(
            1 for row in activity
            if int(row.get("timestamp_raw") or 0) >= int(time.time()) - 7 * 24 * 3600
        )
    tokens = {
        str(value or "").upper()
        for row in events
        for value in [row.get("token_sold"), row.get("token_bought")]
        if value
    }
    if not tokens:
        tokens = {str(row.get("token") or "").upper() for row in activity if row.get("token")}
    incoming = sum(float(row.get("value_usd") or 0) for row in activity if str(row.get("to") or "").lower() == wallet)
    outgoing = sum(float(row.get("value_usd") or 0) for row in activity if str(row.get("from") or "").lower() == wallet)
    estimated_net_flow = incoming - outgoing
    completed_trades = int(quality["completed_trades"])
    profitable_trade_pct = quality["profitable_trade_pct"]
    profitable_score = float(profitable_trade_pct or 0)
    roi_pct = quality["roi_pct"]
    roi_score = normalize_to_100(max(float(roi_pct or 0), 0), 100) if completed_trades else 0.0
    realized_profit = float(quality["realized_profit_estimate"] or 0)
    estimated_profit = quality["estimated_profit"]
    profit_for_score = max(float(estimated_profit or realized_profit or 0), 0) if completed_trades else 0.0
    profit_score = normalize_to_100(profit_for_score, max(float(min_transaction_size) * 10, 100_000))
    size_score = normalize_to_100(avg_trade_size, max(float(min_transaction_size) * 2, 1))
    confidence_score = {"High": 100.0, "Medium": 55.0, "Low": 15.0}.get(str(quality["confidence"]), 15.0)
    trading_frequency_score = float(quality["trading_frequency_score"] or 0)
    whale_score = (
        profitable_score * 0.30
        + roi_score * 0.20
        + trading_frequency_score * 0.20
        + profit_score * 0.15
        + size_score * 0.10
        + confidence_score * 0.05
    )
    if quality["confidence"] == "Low":
        whale_score *= 0.45
    elif quality["confidence"] == "Medium":
        whale_score *= 0.78
    whale_score -= normalize_to_100(largest_trade / max(total_volume, 1) * 100, 90) * 0.05 if trade_count > 1 else 0
    whale_score = round(max(0, min(100, whale_score)), 2)
    copy_quality = _copy_quality(
        str(quality["confidence"]),
        completed_trades,
        float(profitable_trade_pct or 0),
        float(roi_pct or 0),
        float(quality["consistency_score"]),
        float(quality["max_drawdown_estimate"]),
        whale_score,
    )
    profit_note = (
        "Estimated from public DEX swap cycles; confidence improves when both buys and later sells are visible."
        if quality["completed_trades"]
        else "No completed buy/sell cycle detected yet; this wallet is scored as unproven until sell history appears."
    )
    return {
        "wallet": wallet,
        "chain": chain,
        "whale_tier": whale_tier(whale_score),
        "whale_score": whale_score,
        "trend_score": round(min(100, trading_frequency_score * 0.65 + normalize_to_100(recent_activity, 20) * 0.35), 2),
        "net_profit": round(float(quality["estimated_profit"]), 2) if quality["estimated_profit"] is not None else None,
        "estimated_net_flow": round(estimated_net_flow, 2),
        "roi_pct": round(float(roi_pct), 2) if roi_pct is not None else None,
        "estimated_roi_pct": round(float(roi_pct), 2) if roi_pct is not None else None,
        "win_rate": round(float(profitable_trade_pct), 2) if profitable_trade_pct is not None else None,
        "profitable_trade_pct": round(float(profitable_trade_pct), 2) if profitable_trade_pct is not None else None,
        "profitable_swap_pct": round(float(profitable_trade_pct), 2) if profitable_trade_pct is not None else None,
        "adjusted_win_rate": round(float(quality["adjusted_win_rate"]), 2) if quality["adjusted_win_rate"] is not None else None,
        "total_volume": round(total_volume, 2),
        "avg_trade_size": round(avg_trade_size, 2),
        "largest_trade": round(largest_trade, 2),
        "recent_activity": int(recent_activity),
        "trade_count": int(trade_count),
        "unique_tokens": int(len(tokens)),
        "completed_trades": completed_trades,
        "completed_cycles": quality["completed_cycles"],
        "profitable_swaps_count": int(quality["profitable_swaps_count"]),
        "losing_swaps_count": int(quality["losing_swaps_count"]),
        "avg_return_per_trade": round(float(quality["avg_return_per_trade"]), 2) if completed_trades else None,
        "avg_profit_per_completed_trade": round(float(quality["avg_profit_per_completed_trade"]), 2) if quality["avg_profit_per_completed_trade"] is not None else None,
        "recent_profitable_trades": int(quality["recent_profitable_trades"]),
        "trading_frequency": round(float(quality["trading_frequency"]), 4),
        "trading_frequency_score": round(trading_frequency_score, 2),
        "avg_hold_time_hours": round(float(quality["avg_hold_time_hours"]), 2) if completed_trades else None,
        "realized_profit_estimate": round(float(quality["realized_profit_estimate"]), 2) if completed_trades else None,
        "unrealized_profit_estimate": round(float(quality["unrealized_profit_estimate"]), 2) if completed_trades else None,
        "max_drawdown_estimate": round(float(quality["max_drawdown_estimate"]), 2) if completed_trades else None,
        "consistency_score": round(float(quality["consistency_score"]), 2),
        "confidence": str(quality["confidence"]),
        "confidence_level": str(quality["confidence"]),
        "confidence_note": str(quality["confidence_note"]),
        "copy_quality": copy_quality,
        "copy_reasons": quality["copy_reasons"],
        "copy_reason_text": quality["copy_reason_text"],
        "profit_note": profit_note,
        "explorer_url": CHAIN_CONFIGS[chain]["explorer"].format(wallet=wallet),
    }
