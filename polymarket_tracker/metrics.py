from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_wallets(raw: str) -> list[str]:
    wallets: list[str] = []
    for piece in raw.replace(",", "\n").splitlines():
        wallet = piece.strip().lower()
        if wallet and wallet.startswith("0x") and len(wallet) == 42:
            wallets.append(wallet)
    return list(dict.fromkeys(wallets))


@dataclass(frozen=True)
class FilterSettings:
    min_resolved_trades: int = 50
    min_unique_markets: int = 0
    min_win_rate: float = 0
    min_roi: float = 0
    min_total_volume: float = 0
    market_category: str = "All"
    time_period_days: int = 90
    exclude_lucky_big_win: bool = True
    exclude_low_liquidity_trades: bool = True


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def trade_cash_size(trade: dict[str, Any]) -> float:
    size = safe_float(trade.get("size"))
    price = safe_float(trade.get("price"))
    return abs(size * price)


def normalize_to_100(value: float, cap: float, floor: float = 0.0) -> float:
    if cap <= floor:
        return 0.0
    return clamp((value - floor) / (cap - floor) * 100)


def repetitive_size_ratio(trade_sizes: list[float]) -> float:
    if not trade_sizes:
        return 0.0
    rounded_sizes = [round(size, 0) for size in trade_sizes if size > 0]
    if not rounded_sizes:
        return 0.0
    return Counter(rounded_sizes).most_common(1)[0][1] / len(rounded_sizes)


def bot_likeness_warning(
    trade_count: int,
    avg_trade_size: float,
    largest_trade: float,
    same_size_ratio: float,
    win_rate: float,
    total_volume: float,
) -> str:
    warnings: list[str] = []
    if win_rate >= 70 and total_volume < 25000:
        warnings.append("High win rate but low capital - likely not a whale")
    if avg_trade_size < 250:
        warnings.append("Tiny average trade size")
    if trade_count >= 20 and same_size_ratio >= 0.65:
        warnings.append("Repetitive same-size trades")
    if trade_count >= 300 and avg_trade_size < 100:
        warnings.append("High trade count with low average size")
    if largest_trade < 1000:
        warnings.append("No large single trade")
    return "; ".join(warnings)


def bot_likeness_penalty(
    trade_count: int,
    avg_trade_size: float,
    largest_trade: float,
    same_size_ratio: float,
    total_volume: float,
    unique_markets: int,
) -> float:
    penalty = 0.0
    if trade_count >= 300 and avg_trade_size < 100:
        penalty += 35
    if trade_count >= 80 and avg_trade_size < 75:
        penalty += 22
    if avg_trade_size < 50 and trade_count >= 20:
        penalty += 18
    elif avg_trade_size < 150 and trade_count >= 20:
        penalty += 10
    if same_size_ratio >= 0.75 and trade_count >= 20:
        penalty += 25
    elif same_size_ratio >= 0.55 and trade_count >= 20:
        penalty += 12
    if largest_trade < 250 and trade_count >= 15:
        penalty += 14
    if unique_markets <= 2 and trade_count >= 30:
        penalty += 10
    if total_volume < 5000 and trade_count >= 75:
        penalty += 12
    return clamp(penalty, 0, 65)


def whale_tier(score: float) -> str:
    if score >= 85:
        return "Kraken"
    if score >= 70:
        return "Leviathan"
    if score >= 55:
        return "Blue Whale"
    if score >= 38:
        return "Shark"
    return "Dolphin"


def why_ranked_highly(row: dict[str, Any]) -> str:
    reasons: list[str] = []
    if safe_float(row.get("net_profit")) > 0:
        reasons.append(f"${safe_float(row.get('net_profit')):,.0f} realized profit")
    if safe_float(row.get("total_volume")) >= 25000:
        reasons.append(f"${safe_float(row.get('total_volume')):,.0f} traded volume")
    if safe_float(row.get("adjusted_win_rate")) >= 55:
        reasons.append(f"{safe_float(row.get('adjusted_win_rate')):.1f}% adjusted win rate")
    if safe_float(row.get("avg_trade_size")) >= 250:
        reasons.append(f"${safe_float(row.get('avg_trade_size')):,.0f} average position")
    if safe_float(row.get("unique_markets")) >= 8:
        reasons.append(f"{int(safe_float(row.get('unique_markets')))} unique markets")
    if safe_float(row.get("consistency_score")) >= 60:
        reasons.append("consistent realized outcomes")
    if safe_float(row.get("recent_activity")) >= 20:
        reasons.append("strong recent activity")
    if safe_float(row.get("bot_penalty")) >= 20:
        reasons.append("penalized for bot-like patterns")
    return "; ".join(reasons[:4]) or "ranked by balanced profit, volume, activity, and copyability signals"


CATEGORY_KEYWORDS = {
    "All": [],
    "Politics": ["election", "president", "senate", "congress", "trump", "biden", "politic"],
    "Sports": ["nba", "nfl", "mlb", "nhl", "ufc", "soccer", "champions", "tournament", "game"],
    "Crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "coin"],
    "Culture": ["movie", "music", "album", "celebrity", "oscars", "grammy", "culture"],
    "Economics": ["fed", "inflation", "recession", "gdp", "rates", "unemployment", "economy"],
    "Tech": ["ai", "openai", "apple", "google", "microsoft", "tesla", "spacex", "tech"],
    "Finance": ["stock", "nasdaq", "s&p", "dow", "earnings", "finance"],
}


def item_matches_category(item: dict[str, Any], category: str) -> bool:
    keywords = CATEGORY_KEYWORDS.get(category, [])
    if not keywords:
        return True
    haystack = " ".join(
        str(item.get(field, ""))
        for field in ("title", "slug", "eventSlug", "outcome")
    ).lower()
    return any(keyword in haystack for keyword in keywords)


def market_level_positions(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for pos in positions:
        condition_id = str(pos.get("conditionId", "")).lower()
        if not condition_id:
            continue
        item = grouped.setdefault(
            condition_id,
            {
                "conditionId": condition_id,
                "title": pos.get("title", ""),
                "realizedPnl": 0.0,
                "totalBought": 0.0,
                "timestamp": safe_float(pos.get("timestamp")),
                "assets": set(),
            },
        )
        item["realizedPnl"] += safe_float(pos.get("realizedPnl"))
        item["totalBought"] += safe_float(pos.get("totalBought"))
        item["timestamp"] = max(item["timestamp"], safe_float(pos.get("timestamp")))
        if pos.get("asset"):
            item["assets"].add(str(pos.get("asset")))
    return grouped


def filter_items_by_period_and_category(
    rows: list[dict[str, Any]],
    since_ts: int,
    category: str,
) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        timestamp = int(safe_float(row.get("timestamp")))
        if since_ts and timestamp and timestamp < since_ts:
            continue
        if not item_matches_category(row, category):
            continue
        filtered.append(row)
    return filtered


def calculate_entry_timing_scores(
    trades: list[dict[str, Any]],
    price_history_by_asset: dict[str, list[dict[str, Any]]],
    lookahead_hours: int = 24,
) -> tuple[float | None, float | None]:
    if price_history_by_asset is None:
        return None, None
    scores: list[float] = []
    seconds = lookahead_hours * 3600
    for trade in trades:
        asset = str(trade.get("asset") or "")
        history = price_history_by_asset.get(asset) or []
        if not history:
            continue
        ts = int(safe_float(trade.get("timestamp")))
        price = safe_float(trade.get("price"))
        side = str(trade.get("side") or "").upper()
        future_points = [
            safe_float(point.get("p"))
            for point in history
            if ts < int(safe_float(point.get("t"))) <= ts + seconds
        ]
        if not future_points or price <= 0:
            continue
        future = future_points[-1]
        movement = future - price if side == "BUY" else price - future
        scores.append(movement)
    if not scores:
        return None, None
    avg_movement = statistics.mean(scores)
    return avg_movement, clamp(50 + avg_movement * 250)


def score_wallet(
    wallet: str,
    closed_positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    _unused_liquidity_data: dict[str, float] | None = None,
    price_history_by_asset: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    price_history_available = price_history_by_asset is not None
    price_history_by_asset = price_history_by_asset or {}
    markets = market_level_positions(closed_positions)
    resolved = list(markets.values())
    total_pnl = sum(safe_float(item["realizedPnl"]) for item in resolved)
    total_bought = sum(safe_float(item["totalBought"]) for item in resolved)
    roi_pct = (total_pnl / total_bought * 100) if total_bought > 0 else 0.0
    wins = [item for item in resolved if safe_float(item["realizedPnl"]) > 0]
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0.0
    adjusted_win_rate = (len(wins) + 5) / (len(resolved) + 10) * 100 if resolved else 50.0
    trade_sizes = [trade_cash_size(t) for t in trades]
    avg_trade_size = statistics.mean(trade_sizes) if trade_sizes else 0.0
    largest_trade = max(trade_sizes, default=0.0)
    trade_count = len(trade_sizes)
    same_size_ratio = repetitive_size_ratio(trade_sizes)
    total_volume = sum(trade_sizes) or total_bought
    unique_trade_markets = {
        str(trade.get("conditionId") or "").lower()
        for trade in trades
        if trade.get("conditionId")
    }
    unique_markets = len(unique_trade_markets) or len(resolved)
    recent_cutoff = max([int(safe_float(trade.get("timestamp"))) for trade in trades], default=0) - 30 * 24 * 3600
    recent_activity = sum(1 for trade in trades if int(safe_float(trade.get("timestamp"))) >= recent_cutoff) if recent_cutoff > 0 else trade_count
    trade_liquidity_proxy = total_volume / max(unique_markets, 1)
    pnl_series = [safe_float(item["realizedPnl"]) for item in sorted(resolved, key=lambda x: x["timestamp"])]
    drawdown = max_drawdown(pnl_series)
    biggest_win = max([safe_float(item["realizedPnl"]) for item in resolved], default=0.0)
    lucky_big_win_share = biggest_win / total_pnl if total_pnl > 0 else 0.0
    avg_entry_edge, entry_timing_score = calculate_entry_timing_scores(trades, price_history_by_asset)

    pnl_std = statistics.pstdev(pnl_series) if len(pnl_series) > 1 else 0.0
    profit_concentration_score = clamp(100 - lucky_big_win_share * 100)
    consistency = clamp((adjusted_win_rate * 0.55) + clamp(100 - (pnl_std / max(abs(total_pnl), 1) * 180)) * 0.25 + profit_concentration_score * 0.20)
    roi_component = clamp(50 + roi_pct * 2)
    trade_reliability = clamp(math.log10(len(resolved) + 1) / math.log10(251) * 100)
    liquidity_quality = clamp(
        normalize_to_100(total_volume, 250000) * 0.45
        + normalize_to_100(avg_trade_size, 2500) * 0.25
        + normalize_to_100(unique_markets, 100) * 0.15
        + normalize_to_100(recent_activity, 250) * 0.15
    )
    drawdown_penalty = clamp(100 - (drawdown / max(total_bought, 1) * 250))
    copyability = clamp(
        (entry_timing_score if entry_timing_score is not None else 50.0) * 0.20
        + drawdown_penalty * 0.25
        + consistency * 0.20
        + clamp(100 - lucky_big_win_share * 100) * 0.20
        + clamp(100 - max(0, avg_trade_size - 5000) / 100) * 0.15
    )

    score = (
        adjusted_win_rate * 0.40
        + roi_component * 0.20
        + copyability * 0.20
        + liquidity_quality * 0.10
        + trade_reliability * 0.10
    )
    profit_score = normalize_to_100(total_pnl, 30000)
    volume_score = normalize_to_100(total_volume, 300000)
    sizing_score = clamp(normalize_to_100(avg_trade_size, 2500) * 0.65 + normalize_to_100(largest_trade, 10000) * 0.35)
    activity_frequency_score = clamp(
        normalize_to_100(trade_count, 120) * 0.45
        + normalize_to_100(recent_activity, 45) * 0.45
        + normalize_to_100(unique_markets, 30) * 0.10
    )
    diversity_score = normalize_to_100(unique_markets, 35)
    bot_penalty = bot_likeness_penalty(
        trade_count,
        avg_trade_size,
        largest_trade,
        same_size_ratio,
        total_volume,
        unique_markets,
    )
    aggressive_bonus = 0.0
    if largest_trade >= 5000 and avg_trade_size >= 500:
        aggressive_bonus = min(5.0, normalize_to_100(largest_trade, 25000) * 0.05)
    whale_score_raw = (
        profit_score * 0.30
        + adjusted_win_rate * 0.25
        + volume_score * 0.20
        + sizing_score * 0.10
        + consistency * 0.10
        + activity_frequency_score * 0.05
    )
    whale_score = clamp(whale_score_raw + diversity_score * 0.03 + aggressive_bonus - bot_penalty)
    warning = bot_likeness_warning(
        trade_count,
        avg_trade_size,
        largest_trade,
        same_size_ratio,
        win_rate,
        total_volume,
    )

    result = {
        "wallet": wallet,
        "total_pnl": round(total_pnl, 2),
        "net_profit": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "win_rate": round(win_rate, 2),
        "adjusted_win_rate": round(adjusted_win_rate, 2),
        "resolved_markets": len(resolved),
        "total_volume": round(total_volume, 2),
        "avg_trade_size": round(avg_trade_size, 2),
        "largest_trade": round(largest_trade, 2),
        "trade_count": trade_count,
        "same_size_trade_ratio": round(same_size_ratio, 2),
        "trade_liquidity_proxy": round(trade_liquidity_proxy, 2),
        "unique_markets": unique_markets,
        "recent_activity": recent_activity,
        "liquidity_warning": "Liquidity data unavailable for this wallet.",
        "avg_entry_edge_24h": round(avg_entry_edge, 4) if avg_entry_edge is not None else None,
        "entry_timing_score": round(entry_timing_score, 2) if entry_timing_score is not None else None,
        "price_history_available": price_history_available and entry_timing_score is not None,
        "max_drawdown": round(drawdown, 2),
        "profit_score": round(profit_score, 2),
        "volume_score": round(volume_score, 2),
        "position_sizing_score": round(sizing_score, 2),
        "activity_frequency_score": round(activity_frequency_score, 2),
        "bot_penalty": round(bot_penalty, 2),
        "copyability_score": round(copyability, 2),
        "consistency_score": round(consistency, 2),
        "liquidity_quality_score": round(liquidity_quality, 2),
        "trade_count_reliability_score": round(trade_reliability, 2),
        "final_score": round(score, 2),
        "whale_score": round(whale_score, 2),
        "whale_tier": whale_tier(whale_score),
        "bot_likeness_warning": warning,
        "lucky_big_win_share": round(lucky_big_win_share, 2),
        "polygonscan_url": f"https://polygonscan.com/address/{wallet}",
        "polymarket_profile_url": f"https://polymarket.com/profile/{wallet}",
    }
    result["why_ranked_highly"] = why_ranked_highly(result)
    return result


def apply_filters(rows: list[dict[str, Any]], settings: FilterSettings) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        if row["resolved_markets"] < settings.min_resolved_trades:
            continue
        if row["total_pnl"] <= 0:
            continue
        if row["roi_pct"] < settings.min_roi:
            continue
        if row["win_rate"] < settings.min_win_rate:
            continue
        if row["total_volume"] < settings.min_total_volume:
            continue
        if row.get("unique_markets", 0) < settings.min_unique_markets:
            continue
        if settings.exclude_lucky_big_win and row["lucky_big_win_share"] > 0.45:
            continue
        if settings.exclude_low_liquidity_trades and row["liquidity_quality_score"] < 20:
            continue
        filtered.append(row)
    return sorted(filtered, key=lambda item: item["final_score"], reverse=True)
