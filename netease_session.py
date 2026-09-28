"""NetEase session selection, persistence, and cookie helpers."""

from __future__ import annotations

import hashlib
import io
import secrets
import string
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA

from persistence import PersistentStore, utc_now


SESSION_COOKIE_NAMES = ("MUSIC_U", "__csrf")
WEB_QR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
    "Gecko/20100101 Firefox/152.0"
)
_WEAPI_IV = b"0102030405060708"
_WEAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
_WEAPI_BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_WEAPI_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----"""


def parse_cookie(cookie: str) -> dict[str, str]:
    """Parse a Cookie header with last-value-wins semantics."""
    result: dict[str, str] = {}
    if not isinstance(cookie, str):
        return result
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            result[name] = value.strip()
    return result


def normalize_session_cookie(cookie: str) -> str:
    """Keep only the two cookies used by this service."""
    parsed = parse_cookie(cookie)
    return "; ".join(
        f"{name}={parsed[name]}"
        for name in SESSION_COOKIE_NAMES
        if parsed.get(name)
    )


def extract_csrf(cookie: str) -> str:
    return parse_cookie(cookie).get("__csrf", "")


def cookie_fingerprint(cookie: str) -> str:
    normalized = normalize_session_cookie(cookie)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def cookie_from_login_response(
    set_cookie_headers: list[str] | tuple[str, ...] | None,
    body_cookie: Any = None,
    *,
    base_cookie: str = "",
) -> str:
    """Extract the allowlisted session cookies without exposing other response cookies."""
    return normalize_session_cookie(
        merge_cookie_values(base_cookie, set_cookie_headers, body_cookie)
    )


def merge_cookie_values(
    base_cookie: str = "",
    set_cookie_headers: list[str] | tuple[str, ...] | None = None,
    body_cookie: Any = None,
) -> str:
    """Merge Cookie/Set-Cookie values with deterministic last-value-wins semantics."""
    values = parse_cookie(base_cookie)
    for header in set_cookie_headers or ():
        if not isinstance(header, str):
            continue
        first_part = header.split(";", 1)[0]
        name, separator, value = first_part.partition("=")
        if separator and name.strip():
            values[name.strip()] = value.strip()
    if isinstance(body_cookie, str):
        values.update(parse_cookie(body_cookie))
    return "; ".join(f"{name}={value}" for name, value in values.items() if value)


def create_web_qr_context(now_ms: int | None = None) -> dict[str, str]:
    """Create the short-lived browser identity shared by one web QR attempt."""
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    alphabet = string.ascii_letters + string.digits + "-_"

    def web_token(length: int) -> str:
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def cookie_value(value: str) -> str:
        return urllib.parse.quote(value, safe="~()*!.'-_")

    nuid = secrets.token_hex(16)
    cookies = {
        "JSESSIONID-WYYY": web_token(190),
        "_iuqxldmzr_": "33",
        "_ntes_nnid": f"{nuid},{timestamp}",
        "_ntes_nuid": nuid,
        "NMTID": "00" + web_token(39),
        "WEVNSM": "1.0.0",
        "WNMCID": "".join(secrets.choice(string.ascii_lowercase) for _ in range(6))
        + f".{timestamp}.01.0",
    }
    random_number = secrets.randbelow(1_000_000)
    return {
        "chain_id": f"v1_unknown-{random_number}_web_login_{timestamp}",
        "temporary_cookie": "; ".join(
            f"{name}={cookie_value(value)}" for name, value in cookies.items()
        ),
        "user_agent": WEB_QR_USER_AGENT,
        "yd_device_token": "",
    }


def _pkcs7_pad(value: bytes) -> bytes:
    padding = AES.block_size - (len(value) % AES.block_size)
    return value + bytes([padding]) * padding


def _aes_cbc_base64(value: bytes, key: bytes) -> str:
    import base64

    encrypted = AES.new(key, AES.MODE_CBC, _WEAPI_IV).encrypt(_pkcs7_pad(value))
    return base64.b64encode(encrypted).decode("ascii")


def weapi_encrypt(payload: str, secret_key: str | None = None) -> dict[str, str]:
    """Produce the standard NetEase Web API encrypted form fields."""
    key = secret_key or "".join(secrets.choice(_WEAPI_BASE62) for _ in range(16))
    if len(key) != 16 or not key.isascii():
        raise ValueError("The internal NetEase encryption key is invalid.")
    first = _aes_cbc_base64(payload.encode("utf-8"), _WEAPI_PRESET_KEY)
    params = _aes_cbc_base64(first.encode("ascii"), key.encode("ascii"))
    public_key = RSA.import_key(_WEAPI_PUBLIC_KEY)
    reversed_key = key[::-1].encode("ascii")
    encrypted_key = pow(
        int.from_bytes(reversed_key, "big"), public_key.e, public_key.n
    )
    return {"params": params, "encSecKey": format(encrypted_key, "x").zfill(256)}


def qr_png(payload: str) -> bytes:
    """Render a short-lived NetEase login payload without using a third party."""
    if not isinstance(payload, str) or not payload.startswith("https://music.163.com/"):
        raise ValueError("The NetEase QR payload is invalid.")
    if len(payload) > 2048:
        raise ValueError("The NetEase QR payload is too long.")

    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class SessionManager:
    """Select one deployment-level NetEase session without leaking its value."""

    def __init__(
        self,
        store_provider: Callable[[], PersistentStore | None],
        fallback_cookie: str,
        *,
        verification_interval_seconds: int = 900,
    ) -> None:
        self._store_provider = store_provider
        self._fallback_cookie = normalize_session_cookie(fallback_cookie)
        self.verification_interval_seconds = verification_interval_seconds
        self._lock = threading.RLock()
        self._loaded = False
        self._runtime: dict[str, Any] | None = None
        self._invalid_fallback_fingerprint = ""

    def _store(self) -> PersistentStore | None:
        return self._store_provider()

    def _load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            store = self._store()
            self._runtime = store.load_netease_session() if store is not None else None
            self._loaded = True

    @property
    def storage_configured(self) -> bool:
        return self._store() is not None

    def current(self) -> dict[str, Any] | None:
        self._load()
        with self._lock:
            runtime = dict(self._runtime) if self._runtime else None
            if runtime and runtime.get("status") == "logged_out":
                return None
            if runtime and runtime.get("status") == "active":
                cookie = normalize_session_cookie(str(runtime.get("cookie") or ""))
                if parse_cookie(cookie).get("MUSIC_U"):
                    runtime["cookie"] = cookie
                    runtime["source"] = str(runtime.get("source") or "persistent")
                    return runtime

            fallback_fingerprint = cookie_fingerprint(self._fallback_cookie)
            invalid_runtime_fingerprint = cookie_fingerprint(
                str((runtime or {}).get("cookie") or "")
            )
            if (
                parse_cookie(self._fallback_cookie).get("MUSIC_U")
                and fallback_fingerprint != self._invalid_fallback_fingerprint
                and not (
                    runtime
                    and runtime.get("status") == "invalid"
                    and fallback_fingerprint == invalid_runtime_fingerprint
                )
            ):
                return {
                    "cookie": self._fallback_cookie,
                    "csrf": extract_csrf(self._fallback_cookie),
                    "status": "active",
                    "source": "environment",
                    "updated_at": None,
                    "last_verified_at": None,
                    "user_id": None,
                }
            return None

    def get_cookie(self) -> str:
        session = self.current()
        return str(session.get("cookie") or "") if session else ""

    def get_csrf(self) -> str:
        return extract_csrf(self.get_cookie())

    def verification_is_fresh(self, session: dict[str, Any] | None = None) -> bool:
        session = session or self.current()
        value = (session or {}).get("last_verified_at")
        if not isinstance(value, str) or not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        return 0 <= age < self.verification_interval_seconds

    def save(
        self,
        cookie: str,
        *,
        source: str,
        last_verified_at: str | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_session_cookie(cookie)
        if not parse_cookie(normalized).get("MUSIC_U"):
            raise ValueError("The NetEase login response did not include a usable session.")
        store = self._store()
        if store is None:
            raise RuntimeError(
                "NETEASE_SESSION_STORAGE_REQUIRED: Set MCP_STORAGE_PATH to a SQLite file on a persistent volume before QR login."
            )
        record = {
            "cookie": normalized,
            "csrf": extract_csrf(normalized),
            "status": "active",
            "source": source,
            "updated_at": utc_now(),
            "last_verified_at": last_verified_at,
            "user_id": str(user_id) if user_id is not None else None,
        }
        store.save_netease_session(record)
        with self._lock:
            self._runtime = record
            self._loaded = True
        return dict(record)

    def mark_verified(self, user_id: int | str | None = None) -> None:
        session = self.current()
        if session is None:
            return
        verified_at = utc_now()
        store = self._store()
        if store is not None:
            source = (
                "environment_import"
                if session.get("source") == "environment"
                else str(session.get("source") or "persistent")
            )
            self.save(
                str(session["cookie"]),
                source=source,
                last_verified_at=verified_at,
                user_id=user_id,
            )
            return
        with self._lock:
            session["last_verified_at"] = verified_at
            session["user_id"] = str(user_id) if user_id is not None else None
            self._runtime = session
            self._loaded = True

    def invalidate(self) -> None:
        session = self.current()
        if session is None:
            return
        store = self._store()
        invalid = {
            **session,
            "status": "invalid",
            "updated_at": utc_now(),
            "last_verified_at": None,
        }
        if store is not None:
            store.save_netease_session(invalid)
        elif session.get("source") == "environment":
            self._invalid_fallback_fingerprint = cookie_fingerprint(str(session["cookie"]))
        with self._lock:
            self._runtime = invalid
            self._loaded = True

    def logout(self) -> None:
        record = {
            "cookie": "",
            "csrf": "",
            "status": "logged_out",
            "source": "logout",
            "updated_at": utc_now(),
            "last_verified_at": None,
            "user_id": None,
        }
        store = self._store()
        if store is not None:
            store.save_netease_session(record)
        with self._lock:
            self._runtime = record
            self._loaded = True

    def public_status(self) -> dict[str, Any]:
        self._load()
        session = self.current()
        runtime_status = str((self._runtime or {}).get("status") or "missing")
        if session is None:
            return {
                "status": "logged_out" if runtime_status == "logged_out" else "missing",
                "source": None,
                "storage_configured": self.storage_configured,
                "last_verified_at": None,
            }
        return {
            "status": "active" if self.verification_is_fresh(session) else "unverified",
            "source": session.get("source"),
            "storage_configured": self.storage_configured,
            "last_verified_at": session.get("last_verified_at"),
        }
