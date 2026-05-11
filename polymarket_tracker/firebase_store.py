from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import requests


FIREBASE_TIMEOUT_SECONDS = 10
FIREBASE_CONNECTION_ERROR = "Could not connect to Firebase. Check API key, project ID, or Firebase status."

DEFAULT_USER_SETTINGS = {
    "min_trade_size": 250.0,
    "popup_alerts_enabled": True,
    "whale_mode_enabled": True,
}


class FirebaseError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AuthSession:
    id_token: str
    refresh_token: str | None
    user_id: str
    email: str
    expires_at: int

    def to_dict(self) -> dict:
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "user_id": self.user_id,
            "email": self.email,
            "expires_at": self.expires_at,
        }


class FirebaseStore:
    def __init__(self, api_key: str, project_id: str):
        self.api_key = api_key.strip()
        self.project_id = project_id.strip()
        self.auth_url = "https://identitytoolkit.googleapis.com/v1"
        self.token_url = "https://securetoken.googleapis.com/v1"
        self.firestore_url = (
            f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
            "/databases/(default)/documents"
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            return requests.request(method, url, timeout=FIREBASE_TIMEOUT_SECONDS, **kwargs)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
        ) as exc:
            raise FirebaseError(FIREBASE_CONNECTION_ERROR) from exc

    def _handle_response(self, response: requests.Response):
        if response.ok:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return None
        raise FirebaseError(self._error_message(response), response.status_code)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Firebase request failed with status {response.status_code}."
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or "Firebase request failed.")
        if isinstance(payload, dict):
            return str(payload.get("message") or f"Firebase request failed with status {response.status_code}.")
        return f"Firebase request failed with status {response.status_code}."

    @staticmethod
    def _session_from_payload(payload: dict) -> AuthSession:
        id_token = str(payload.get("idToken") or payload.get("id_token") or "")
        user_id = str(payload.get("localId") or payload.get("user_id") or "")
        if not id_token or not user_id:
            raise FirebaseError("Firebase Auth did not return a valid user session.")
        expires_in = int(payload.get("expiresIn") or payload.get("expires_in") or 3600)
        return AuthSession(
            id_token=id_token,
            refresh_token=payload.get("refreshToken") or payload.get("refresh_token"),
            user_id=user_id,
            email=str(payload.get("email") or ""),
            expires_at=int(time.time()) + expires_in,
        )

    def sign_up(self, email: str, password: str) -> AuthSession:
        response = self._request(
            "POST",
            f"{self.auth_url}/accounts:signUp",
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
            json={"email": email, "password": password, "returnSecureToken": True},
        )
        return self._session_from_payload(self._handle_response(response) or {})

    def sign_in(self, email: str, password: str) -> AuthSession:
        response = self._request(
            "POST",
            f"{self.auth_url}/accounts:signInWithPassword",
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
            json={"email": email, "password": password, "returnSecureToken": True},
        )
        return self._session_from_payload(self._handle_response(response) or {})

    def refresh_session(self, refresh_token: str) -> AuthSession:
        response = self._request(
            "POST",
            f"{self.token_url}/token",
            params={"key": self.api_key},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        return self._session_from_payload(self._handle_response(response) or {})

    def logout(self) -> None:
        return None

    def _doc_url(self, *segments: str) -> str:
        path = "/".join(quote(str(segment), safe="") for segment in segments)
        return f"{self.firestore_url}/{path}"

    @staticmethod
    def _auth_headers(id_token: str) -> dict:
        return {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _now_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _fields(cls, values: dict) -> dict:
        fields = {}
        for key, value in values.items():
            if value is None:
                fields[key] = {"nullValue": None}
            elif isinstance(value, bool):
                fields[key] = {"booleanValue": value}
            elif isinstance(value, int) and not isinstance(value, bool):
                fields[key] = {"integerValue": str(value)}
            elif isinstance(value, float):
                fields[key] = {"doubleValue": value}
            elif key.endswith("_at"):
                fields[key] = {"timestampValue": str(value)}
            else:
                fields[key] = {"stringValue": str(value)}
        return {"fields": fields}

    @classmethod
    def _value_from_field(cls, field: dict):
        if "stringValue" in field:
            return field["stringValue"]
        if "doubleValue" in field:
            return float(field["doubleValue"])
        if "integerValue" in field:
            return int(field["integerValue"])
        if "booleanValue" in field:
            return bool(field["booleanValue"])
        if "timestampValue" in field:
            return field["timestampValue"]
        return None

    @classmethod
    def _document_fields(cls, document: dict) -> dict:
        return {
            key: cls._value_from_field(value)
            for key, value in (document.get("fields") or {}).items()
        }

    def fetch_watchlist(self, id_token: str, user_id: str) -> list[dict]:
        response = self._request(
            "GET",
            self._doc_url("users", user_id, "watchlist"),
            headers=self._auth_headers(id_token),
        )
        if response.status_code == 404:
            return []
        payload = self._handle_response(response) or {}
        rows = [self._normalize_watchlist_doc(doc, user_id) for doc in payload.get("documents", [])]
        return sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)

    def upsert_watchlist(
        self,
        id_token: str,
        user_id: str,
        wallet_address: str,
        wallet_label: str | None = None,
    ) -> list[dict]:
        wallet = wallet_address.lower()
        now = self._now_timestamp()
        response = self._request(
            "PATCH",
            self._doc_url("users", user_id, "watchlist", wallet),
            headers=self._auth_headers(id_token),
            json=self._fields(
                {
                    "wallet_address": wallet,
                    "wallet_label": wallet_label or "",
                    "created_at": now,
                }
            ),
        )
        document = self._handle_response(response) or {}
        return [self._normalize_watchlist_doc(document, user_id)]

    def delete_watchlist_wallet(self, id_token: str, user_id: str, wallet_address: str) -> None:
        response = self._request(
            "DELETE",
            self._doc_url("users", user_id, "watchlist", wallet_address.lower()),
            headers=self._auth_headers(id_token),
        )
        if response.status_code != 404:
            self._handle_response(response)

    def _normalize_watchlist_doc(self, document: dict, user_id: str) -> dict:
        fields = self._document_fields(document)
        wallet = str(fields.get("wallet_address") or document.get("name", "").rsplit("/", 1)[-1]).lower()
        return {
            "id": wallet,
            "user_id": user_id,
            "wallet": wallet,
            "wallet_address": wallet,
            "wallet_label": fields.get("wallet_label") or "",
            "created_at": fields.get("created_at"),
        }

    def fetch_user_settings(self, id_token: str, user_id: str) -> dict:
        response = self._request(
            "GET",
            self._doc_url("users", user_id, "settings", "main"),
            headers=self._auth_headers(id_token),
        )
        if response.status_code == 404:
            return self.upsert_user_settings(id_token, user_id, DEFAULT_USER_SETTINGS)
        document = self._handle_response(response) or {}
        return self._normalize_settings(self._document_fields(document), user_id)

    def upsert_user_settings(self, id_token: str, user_id: str, settings: dict) -> dict:
        now = self._now_timestamp()
        response = self._request(
            "PATCH",
            self._doc_url("users", user_id, "settings", "main"),
            headers=self._auth_headers(id_token),
            json=self._fields(
                {
                    "min_trade_size": float(settings.get("min_trade_size", DEFAULT_USER_SETTINGS["min_trade_size"])),
                    "popup_alerts_enabled": bool(
                        settings.get("popup_alerts_enabled", DEFAULT_USER_SETTINGS["popup_alerts_enabled"])
                    ),
                    "whale_mode_enabled": bool(
                        settings.get("whale_mode_enabled", DEFAULT_USER_SETTINGS["whale_mode_enabled"])
                    ),
                    "created_at": settings.get("created_at") or now,
                    "updated_at": now,
                }
            ),
        )
        document = self._handle_response(response) or {}
        return self._normalize_settings(self._document_fields(document), user_id)

    @staticmethod
    def _normalize_settings(fields: dict, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "min_trade_size": float(fields.get("min_trade_size") or DEFAULT_USER_SETTINGS["min_trade_size"]),
            "popup_alerts_enabled": bool(
                fields.get("popup_alerts_enabled", DEFAULT_USER_SETTINGS["popup_alerts_enabled"])
            ),
            "whale_mode_enabled": bool(fields.get("whale_mode_enabled", DEFAULT_USER_SETTINGS["whale_mode_enabled"])),
            "created_at": fields.get("created_at"),
            "updated_at": fields.get("updated_at"),
        }
