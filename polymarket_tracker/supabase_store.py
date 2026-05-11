from __future__ import annotations

import time
from dataclasses import dataclass

import requests


SUPABASE_TIMEOUT_SECONDS = 10
SUPABASE_CONNECTION_ERROR = "Could not connect to Supabase. Check URL/key or Supabase project status."

DEFAULT_USER_SETTINGS = {
    "min_trade_size": 250.0,
    "popup_alerts_enabled": True,
    "whale_mode_enabled": True,
}


class SupabaseError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    refresh_token: str | None
    user_id: str
    email: str
    expires_at: int

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "user_id": self.user_id,
            "email": self.email,
            "expires_at": self.expires_at,
        }


class SupabaseStore:
    def __init__(self, url: str, anon_key: str):
        self.url = url.rstrip("/")
        self.anon_key = anon_key

    def _auth_headers(self) -> dict:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.anon_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _rest_headers(self, access_token: str, prefer: str | None = None) -> dict:
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        try:
            return requests.request(
                method,
                f"{self.url}{endpoint}",
                timeout=SUPABASE_TIMEOUT_SECONDS,
                **kwargs,
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as exc:
            raise SupabaseError(SUPABASE_CONNECTION_ERROR) from exc

    def _handle_response(self, response: requests.Response):
        if response.ok:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return None
        raise SupabaseError(self._error_message(response), response.status_code)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Supabase request failed with status {response.status_code}."
        if isinstance(payload, dict):
            return (
                payload.get("msg")
                or payload.get("message")
                or payload.get("error_description")
                or payload.get("error")
                or f"Supabase request failed with status {response.status_code}."
            )
        return f"Supabase request failed with status {response.status_code}."

    @staticmethod
    def _session_from_payload(payload: dict) -> AuthSession | None:
        session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        access_token = session_payload.get("access_token")
        if not access_token:
            return None
        user = payload.get("user") or session_payload.get("user") or {}
        user_id = str(user.get("id") or "")
        email = str(user.get("email") or "")
        if not user_id:
            return None
        expires_in = int(session_payload.get("expires_in") or 3600)
        return AuthSession(
            access_token=str(access_token),
            refresh_token=session_payload.get("refresh_token"),
            user_id=user_id,
            email=email,
            expires_at=int(time.time()) + expires_in,
        )

    def sign_up(self, email: str, password: str) -> AuthSession | None:
        response = self._request(
            "POST",
            "/auth/v1/signup",
            headers=self._auth_headers(),
            json={"email": email, "password": password},
        )
        payload = self._handle_response(response) or {}
        return self._session_from_payload(payload)

    def sign_in(self, email: str, password: str) -> AuthSession:
        response = self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "password"},
            headers=self._auth_headers(),
            json={"email": email, "password": password},
        )
        payload = self._handle_response(response) or {}
        session = self._session_from_payload(payload)
        if not session:
            raise SupabaseError("Login succeeded but no user session was returned.")
        return session

    def refresh_session(self, refresh_token: str) -> AuthSession:
        response = self._request(
            "POST",
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            headers=self._auth_headers(),
            json={"refresh_token": refresh_token},
        )
        payload = self._handle_response(response) or {}
        session = self._session_from_payload(payload)
        if not session:
            raise SupabaseError("Could not refresh Supabase session.")
        return session

    def logout(self, access_token: str) -> None:
        response = self._request(
            "POST",
            "/auth/v1/logout",
            headers=self._rest_headers(access_token),
        )
        self._handle_response(response)

    def test_connection(self) -> dict:
        response = self._request(
            "GET",
            "/auth/v1/settings",
            headers=self._auth_headers(),
        )
        return self._handle_response(response) or {}

    def fetch_watchlist(self, access_token: str, user_id: str) -> list[dict]:
        response = self._request(
            "GET",
            "/rest/v1/watchlists",
            headers=self._rest_headers(access_token),
            params={
                "select": "id,user_id,wallet_address,wallet_label,created_at",
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
            },
        )
        rows = self._handle_response(response) or []
        return [self._normalize_watchlist_row(row) for row in rows]

    def upsert_watchlist(
        self,
        access_token: str,
        user_id: str,
        wallet_address: str,
        wallet_label: str | None = None,
    ) -> list[dict]:
        response = self._request(
            "POST",
            "/rest/v1/watchlists",
            headers=self._rest_headers(access_token, "resolution=merge-duplicates,return=representation"),
            params={"on_conflict": "user_id,wallet_address"},
            json={
                "user_id": user_id,
                "wallet_address": wallet_address.lower(),
                "wallet_label": wallet_label or None,
            },
        )
        rows = self._handle_response(response) or []
        return [self._normalize_watchlist_row(row) for row in rows]

    def delete_watchlist_wallet(self, access_token: str, user_id: str, wallet_address: str) -> None:
        response = self._request(
            "DELETE",
            "/rest/v1/watchlists",
            headers=self._rest_headers(access_token),
            params={
                "user_id": f"eq.{user_id}",
                "wallet_address": f"eq.{wallet_address.lower()}",
            },
        )
        self._handle_response(response)

    @staticmethod
    def _normalize_watchlist_row(row: dict) -> dict:
        wallet = str(row.get("wallet_address") or row.get("wallet") or "").lower()
        return {
            "id": row.get("id"),
            "user_id": row.get("user_id"),
            "wallet": wallet,
            "wallet_address": wallet,
            "wallet_label": row.get("wallet_label") or "",
            "created_at": row.get("created_at"),
        }

    def fetch_user_settings(self, access_token: str, user_id: str) -> dict:
        response = self._request(
            "GET",
            "/rest/v1/user_settings",
            headers=self._rest_headers(access_token),
            params={
                "select": "user_id,min_trade_size,popup_alerts_enabled,whale_mode_enabled,created_at,updated_at",
                "user_id": f"eq.{user_id}",
                "limit": "1",
            },
        )
        rows = self._handle_response(response) or []
        if not rows:
            return self.upsert_user_settings(access_token, user_id, DEFAULT_USER_SETTINGS)
        return self._normalize_settings(rows[0])

    def upsert_user_settings(self, access_token: str, user_id: str, settings: dict) -> dict:
        payload = {
            "user_id": user_id,
            "min_trade_size": float(settings.get("min_trade_size", DEFAULT_USER_SETTINGS["min_trade_size"])),
            "popup_alerts_enabled": bool(
                settings.get("popup_alerts_enabled", DEFAULT_USER_SETTINGS["popup_alerts_enabled"])
            ),
            "whale_mode_enabled": bool(settings.get("whale_mode_enabled", DEFAULT_USER_SETTINGS["whale_mode_enabled"])),
        }
        response = self._request(
            "POST",
            "/rest/v1/user_settings",
            headers=self._rest_headers(access_token, "resolution=merge-duplicates,return=representation"),
            params={"on_conflict": "user_id"},
            json=payload,
        )
        rows = self._handle_response(response) or []
        if rows:
            return self._normalize_settings(rows[0])
        return self._normalize_settings(payload)

    @staticmethod
    def _normalize_settings(row: dict) -> dict:
        return {
            "user_id": row.get("user_id"),
            "min_trade_size": float(row.get("min_trade_size") or DEFAULT_USER_SETTINGS["min_trade_size"]),
            "popup_alerts_enabled": bool(
                row.get("popup_alerts_enabled", DEFAULT_USER_SETTINGS["popup_alerts_enabled"])
            ),
            "whale_mode_enabled": bool(row.get("whale_mode_enabled", DEFAULT_USER_SETTINGS["whale_mode_enabled"])),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
