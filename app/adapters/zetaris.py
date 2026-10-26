"""Zetaris data-layer adapter: one SQL statement per call.

Two channels, chosen by configuration:

  proxy  (when ZETARIS_BASE_URL is set; the route used by Zetaris's own getting-started scripts)
         POST {ZETARIS_BASE_URL}/api/proxy/sql-editor/sqls/run-query   body {"sql", "limit", ["engineId"]}
         Auth: ZETARIS_API_KEY as the bearer token, or ZETARIS_USERNAME/ZETARIS_PASSWORD via POST /api/auth/login.
  rest   (otherwise) POST {ZETARIS_REST_URL}/sql-editor/sqls/run   body {"queryId", "sql", "source", "limit"}

Both send a bearer token, X-Org-ID and a fresh X-Request-ID. Retries once on timeouts, connection errors and
5xx/429, then raises a structured DataError. Neither channel has been run against a live Zetaris by the author.
"""
from __future__ import annotations

import os
import time
import uuid

import requests

from .. import config
from ..errors import DataError
from ..sqlguard import assert_read_only

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def row_limit() -> int:
    try:
        return max(1, int(config.get("ZETARIS_ROW_LIMIT", "1000")))
    except ValueError:
        return 1000


def _extract_rows(payload) -> list:
    if isinstance(payload, dict):
        data = payload.get("data", payload.get("rows"))
        if isinstance(data, dict):
            data = data.get("rows", data.get("data"))
    else:
        data = None
    if not isinstance(data, list):
        raise DataError("Zetaris returned a response without a list of rows (expected key 'data').",
                        stage="zetaris", details={"keys": sorted(payload) if isinstance(payload, dict) else None})
    return data


_login_token: dict = {}     # base url -> token obtained with username/password


def _proxy_token(base: str, org_id: str, timeout: float) -> str:
    """ZETARIS_API_KEY if set; otherwise log in once with ZETARIS_USERNAME / ZETARIS_PASSWORD."""
    key = config.get("ZETARIS_API_KEY")
    if key:
        return key
    if base in _login_token:
        return _login_token[base]
    user, password = config.get("ZETARIS_USERNAME"), config.get("ZETARIS_PASSWORD")
    if not (user and password):
        raise DataError("Set ZETARIS_API_KEY, or ZETARIS_USERNAME and ZETARIS_PASSWORD.", stage="zetaris")
    try:
        resp = requests.post(base + "/api/auth/login", json={"username": user, "password": password},
                             headers={"X-Request-ID": str(uuid.uuid4()), "X-Org-ID": org_id}, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise DataError(f"Zetaris login unreachable ({type(exc).__name__}).", stage="zetaris") from exc
    if resp.status_code >= 400:
        raise DataError(f"Zetaris login returned HTTP {resp.status_code}.", stage="zetaris",
                        details={"http_status": resp.status_code})
    try:
        body = resp.json()
    except ValueError as exc:
        raise DataError("Zetaris login returned a non-JSON response.", stage="zetaris") from exc
    token = next((body[k] for k in ("idToken", "token", "accessToken") if isinstance(body, dict) and body.get(k)), None)
    if not token:
        raise DataError("Zetaris login did not return an access token.", stage="zetaris")
    _login_token[base] = token
    return token


def run_sql(sql: str, limit: int | None = None) -> list:
    assert_read_only(sql)
    limit = limit or row_limit()
    timeout = config.get_float("ZETARIS_TIMEOUT", 60.0)
    proxy_base = (config.get("ZETARIS_BASE_URL") or "").rstrip("/")
    try:
        org_id = os.environ["ZETARIS_ORG_ID"]
        if proxy_base:
            base, api_key = proxy_base, _proxy_token(proxy_base, org_id, timeout)
            url = base + "/api/proxy/sql-editor/sqls/run-query"
            body = {"sql": sql, "limit": limit}
            if config.get("ZETARIS_ENGINE_ID"):
                body["engineId"] = config.get("ZETARIS_ENGINE_ID")
        else:
            base = os.environ["ZETARIS_REST_URL"].rstrip("/")
            api_key = os.environ["ZETARIS_API_KEY"]
            url = base + "/sql-editor/sqls/run"
            body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
    except KeyError as exc:
        raise DataError(f"Missing Zetaris setting: {exc.args[0]}", stage="zetaris") from exc
    attempts, last = 2, ""
    for attempt in range(attempts):
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "X-Org-ID": org_id,
                    "X-Request-ID": str(uuid.uuid4()),
                },
                json=body,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = f"unreachable ({type(exc).__name__})"
        else:
            if response.status_code in RETRYABLE_STATUS:
                last = f"HTTP {response.status_code}"
            elif response.status_code >= 400:
                raise DataError(f"Zetaris returned HTTP {response.status_code}: {response.text[:300]}",
                                stage="zetaris", details={"http_status": response.status_code})
            else:
                try:
                    return _extract_rows(response.json())
                except ValueError as exc:
                    raise DataError("Zetaris returned a non-JSON response.", stage="zetaris") from exc
        if attempt + 1 < attempts:
            time.sleep(config.get_float("ZETARIS_BACKOFF_SECONDS", 1.0) * (attempt + 1))
    raise DataError(f"Zetaris failed after {attempts} attempts: {last}", stage="zetaris",
                    details={"attempts": attempts})
