"""
Upstox authentication helpers.

Two ways to get an access token, both supported by the app:

1. MANUAL (fastest)  - click "Generate" next to your app on
   https://account.upstox.com/developer/apps and paste the token into the app.
   No redirect URL involved.

2. OAUTH (one-click) - register a redirect URL (this app serves /callback), then:
     GET  /v2/login/authorization/dialog?response_type=code&client_id=...&redirect_uri=...&state=...
     -> Upstox sends the browser back to  <redirect_uri>?code=XXXX
     POST /v2/login/authorization/token   (code + client_id + client_secret + redirect_uri)
Everything here is standard library only.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

_API_ROOT = (os.environ.get("UPSTOX_API_ROOT") or "https://api.upstox.com").rstrip("/")
DIALOG_URL = _API_ROOT + "/v2/login/authorization/dialog"
TOKEN_URL = _API_ROOT + "/v2/login/authorization/token"


# ------------------------------------------------------------------ credentials
def cred_path(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, "credentials.json")


def load_credentials(runtime_dir: str) -> dict:
    try:
        with open(cred_path(runtime_dir)) as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    return {"api_key": str(data.get("api_key") or "").strip(),
            "api_secret": str(data.get("api_secret") or "").strip(),
            "redirect_uri": str(data.get("redirect_uri") or "").strip(),
            "state": str(data.get("state") or "").strip()}


def save_credentials(runtime_dir: str, creds: dict) -> str:
    creds = dict(creds or {})
    creds.setdefault("state", secrets.token_urlsafe(12))
    p = cred_path(runtime_dir)
    with open(p, "w") as f:
        json.dump(creds, f)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return creds["state"]


# ------------------------------------------------------------------- redirect url
def self_redirect_url(host_header: str, proto: str = "", path: str = "/callback") -> str:
    """Work out this app's own public /callback URL from the request's Host header.
    Behind a proxy (like the hosted preview) it returns the public https URL; on a
    laptop it returns http://localhost:<port>/callback."""
    host = (host_header or "").strip()
    if not host:
        return ""
    if proto in ("http", "https"):
        scheme = proto
    elif host.startswith("localhost") or host.startswith("127.0.0.1") or host.startswith("0.0.0.0"):
        scheme = "http"
    else:
        scheme = "https"
    return f"{scheme}://{host}{path}"


def login_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    if state:
        params["state"] = state
    return DIALOG_URL + "?" + urllib.parse.urlencode(params)


# ----------------------------------------------------------------------- exchange
def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str,
                  timeout: float = 25.0) -> tuple[str, dict]:
    """Swap a single-use auth code for an access token. Returns (token, raw_response)."""
    form = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=form,
        headers={"accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "fno-screener/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            body = {"errors": [{"message": f"HTTP {e.code}"}]}
        raise AuthError(_error_text(body, e.code), e.code)
    except Exception as e:                       # noqa: BLE001
        raise AuthError(f"Could not reach Upstox: {e}", None)
    token = (body.get("access_token") or "").strip()
    if not token:
        raise AuthError(_error_text(body, None), None)
    return token, body


ERROR_HINTS = {
    "UDAPI100069": "API key or API secret is wrong — copy them again from your Upstox developer app.",
    "UDAPI100070": "This redirect URI doesn't match the one registered on your Upstox app "
                   "(exact match required, including http/https and the /callback path).",
    "UDAPI100057": "The auth code is invalid — codes are single-use and expire quickly. "
                   "Run the login flow again to get a fresh one.",
    "UDAPI100050": "Access token is invalid or expired.",
}


def _error_text(body: dict, status) -> str:
    if isinstance(body, dict) and body.get("errors"):
        e = body["errors"][0]
        code = (e or {}).get("errorCode") or ""
        msg = (e or {}).get("message") or ""
        hint = ERROR_HINTS.get(code, "")
        return f"{code + ': ' if code else ''}{msg}" + (f"  ({hint})" if hint else "")
    if body.get("error_description"):
        return str(body["error_description"])
    return f"Token exchange failed (HTTP {status})"


class AuthError(Exception):
    """Carries Upstox's own error text plus the HTTP status, with a clean __str__."""

    def __init__(self, msg: str, status=None):
        super().__init__(msg)
        self.status = status
        self.msg = msg

    def __str__(self) -> str:
        return self.msg
