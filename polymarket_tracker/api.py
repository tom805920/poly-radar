from __future__ import annotations

import time
import logging
from typing import Any

import requests
from requests import RequestException


DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
MAX_OFFSET = 3000
PRICE_HISTORY_DAY_SECONDS = 24 * 3600
PRICE_HISTORY_FALLBACK_SECONDS = 6 * 3600
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)


class PolymarketAPIError(RuntimeError):
    """Raised when a public Polymarket API request fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PolymarketClient:
    def __init__(self, timeout: int = REQUEST_TIMEOUT_SECONDS, pause_seconds: float = 0.08) -> None:
        self.session = requests.Session()
        self.timeout = timeout
        self.pause_seconds = pause_seconds

    def _request_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_error: RequestException | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.session.get(
                    url,
                    params=params or {},
                    headers=REQUEST_HEADERS,
                    timeout=self.timeout,
                )
            except RequestException as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    raise PolymarketAPIError(f"Request failed for {url}: {exc}") from exc
                time.sleep(0.25 * (attempt + 1))
                continue
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code >= 400:
                logger.warning("API request failed: %s status=%s", url, response.status_code)
                raise PolymarketAPIError(
                    f"{response.status_code} from {url}: API request failed",
                    status_code=response.status_code,
                )
            if "application/json" not in content_type:
                logger.warning("Skipping non-JSON API response: %s content-type=%s", url, content_type)
                raise PolymarketAPIError(
                    f"Non-JSON response from {url}; skipping request",
                    status_code=response.status_code,
                )
            time.sleep(self.pause_seconds)
            try:
                return response.json()
            except ValueError as exc:
                logger.warning("Skipping invalid JSON API response: %s", url)
                raise PolymarketAPIError(f"Invalid JSON response from {url}; skipping request") from exc
        raise PolymarketAPIError(f"Request failed for {url}: {last_error}")

    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{base_url}{path}"
        return self._request_json(url, params=params)

    def _get_url(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_json(url, params=params)

    def fetch_closed_positions(self, wallet: str, limit: int = 50, max_rows: int = 1000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for offset in range(0, max_rows, limit):
            batch = self._get(
                DATA_API,
                "/closed-positions",
                {
                    "user": wallet,
                    "limit": limit,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
        return rows

    def fetch_trades(self, wallet: str, limit: int = 500, max_rows: int = 5000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        safe_max_rows = min(max_rows, MAX_OFFSET)
        for offset in range(0, safe_max_rows, limit):
            if offset >= MAX_OFFSET:
                break
            try:
                batch = self.fetch_trades_page(wallet=wallet, limit=limit, offset=offset)
            except PolymarketAPIError as exc:
                if exc.status_code == 400:
                    break
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
        return rows

    def fetch_recent_trades(self, limit: int = 1000, max_rows: int = 5000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        safe_max_rows = min(max_rows, MAX_OFFSET)
        for offset in range(0, safe_max_rows, limit):
            if offset >= MAX_OFFSET:
                break
            try:
                batch = self.fetch_trades_page(limit=limit, offset=offset)
            except PolymarketAPIError as exc:
                if exc.status_code == 400:
                    break
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
        return rows

    def fetch_trades_page(
        self,
        limit: int = 500,
        offset: int = 0,
        wallet: str | None = None,
    ) -> list[dict[str, Any]]:
        if offset >= MAX_OFFSET:
            return []
        params: dict[str, Any] = {"limit": limit, "offset": offset, "takerOnly": "false"}
        if wallet:
            params["user"] = wallet
        return list(self._get_url(f"{DATA_API}/trades", params=params) or [])

    def fetch_price_history(
        self,
        asset: str,
        start_ts: int,
        end_ts: int,
        interval: str = "1h",
    ) -> list[dict[str, Any]]:
        if not asset or start_ts <= 0 or end_ts <= start_ts:
            return []
        if end_ts - start_ts > PRICE_HISTORY_DAY_SECONDS:
            raise PolymarketAPIError(
                "Refusing to request prices-history range longer than 1 day",
                status_code=400,
            )
        payload = self._get(
            CLOB_API,
            "/prices-history",
            {
                "market": asset,
                "startTs": start_ts,
                "endTs": end_ts,
                "interval": interval,
                "fidelity": 60,
            },
        )
        return list(payload.get("history") or [])
