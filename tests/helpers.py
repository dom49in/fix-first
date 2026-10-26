"""Test helpers: environment switching, ground truth from the CSV, and local stand-ins for the sponsor services.

The stand-ins (FakeZetaris, FakeNightshift) exist ONLY inside the test suite so the live-mode code paths
(HTTP contract, headers, retries, row shapes, row caps) can be exercised without credentials. They are never
imported by the application, and nothing they return is used as an application output.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV_KEYS = ["SAMPLE_MODE", "MEMORY_DIR", "LOG_DIR", "METERLESS_BASE_URL", "METERLESS_RUNTIME_TOKEN", "METERLESS_MODEL",
            "LLM_MODE", "ZETARIS_REST_URL", "ZETARIS_API_KEY", "ZETARIS_ORG_ID", "ZETARIS_TABLE", "ZETARIS_ROW_LIMIT",
            "LLM_BACKOFF_SECONDS", "ZETARIS_BACKOFF_SECONDS", "MEMORY_ENABLED", "HMEM_THRESHOLD", "LLM_TIMEOUT", "LOG_STDOUT",
            "ZETARIS_BASE_URL", "ZETARIS_USERNAME", "ZETARIS_PASSWORD", "ZETARIS_ENGINE_ID", 
            "ZETARIS_TIMEOUT", "METERLESS_APP", "ZETARIS_POLICY_TABLE", "ZETARIS_HOLIDAY_TABLE",
            "ZETARIS_WEATHER_TABLE", "PREFLIGHT_REQUIRE_ALL", "PREFLIGHT_REQUIRE_POLICY", "ZETARIS_WORKSET_LIMIT"]


@contextlib.contextmanager
def env(**overrides):
    """Set (or with None, remove) environment variables; everything else in ENV_KEYS is cleared first."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["LOG_STDOUT"] = "false"        # keep test output readable; stdout echo is tested explicitly
    for k, v in overrides.items():
        if v is not None:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def tempdir():
    with tempfile.TemporaryDirectory(prefix="fixfirst-test-") as d:
        yield Path(d)


# -- ground truth, computed with the csv module only (no code from app/) ---------------------------------------
CSV_PATH = ROOT / "data" / "work_orders.csv"
OPEN = {"OPEN", "01-ASSIGNED", "02-INPROGRESS", "PENDING"}


def load_rows() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def open_rows(rows=None) -> list[dict]:
    return [r for r in (rows or load_rows()) if r["status"] in OPEN]


# -- local stand-ins ---------------------------------------------------------------------------------------------
class _Server:
    def __init__(self, handler_cls):
        self.requests: list[dict] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.owner = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class _ZetarisHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        owner = self.server.owner
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        owner.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if owner.fail_status and owner.fail_count > 0:
            owner.fail_count -= 1
            return self._send(owner.fail_status, {"error": "scripted failure"})
        if self.path == "/api/auth/login":
            if body.get("username") == "u" and body.get("password") == "p":
                return self._send(200, {"idToken": "login-token"})
            return self._send(401, {"error": "bad login"})
        if self.path not in ("/api/v1.0/sql-editor/sqls/run", "/api/proxy/sql-editor/sqls/run-query"):
            return self._send(404, {"error": "no such route"})
        from app import sample_store
        rows = sample_store.run_sql(body["sql"])
        rows = rows[: owner.row_cap] if owner.row_cap else rows
        shape = owner.shape
        if shape == "lists":
            data = [list(r.values()) for r in rows]
        elif shape == "upper_dicts":
            data = [{k.upper(): v for k, v in r.items()} for r in rows]
        elif shape == "other_keys":
            data = [{f"col{i}": v for i, v in enumerate(r.values())} for r in rows]
        else:
            data = rows
        self._send(200, {"data": data})

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeZetaris(_Server):
    def __init__(self, shape="dicts", row_cap=None, fail_status=None, fail_count=0):
        self.shape, self.row_cap, self.fail_status, self.fail_count = shape, row_cap, fail_status, fail_count
        super().__init__(_ZetarisHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/v1.0"

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


class _NightshiftHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        owner = self.server.owner
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        owner.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if owner.fail_first > 0:
            owner.fail_first -= 1
            return self._send(503, {"error": "busy"})
        if self.headers.get("Authorization") != "Bearer " + owner.token:
            return self._send(401, {"error": "bad token"})
        from app import offline
        try:
            answer = json.dumps(offline.respond(body["messages"]))
        except Exception:                      # not one of the app's role prompts: answer with a trivial JSON object
            answer = json.dumps({"ok": True})
        if owner.prose_first > 0:
            owner.prose_first -= 1
            answer = "Sure! Here is my thinking in prose, no JSON."
        content = answer
        self._send(200, {"model": owner.model, "choices": [{"message": {"content": content}}],
                         "usage": {"prompt_tokens": sum(len(m["content"]) for m in body["messages"]) // 4,
                                   "completion_tokens": len(content) // 4}})

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeNightshift(_Server):
    """OpenAI-compatible stand-in. Its answers come from app/offline.py's rules purely so that the HTTP path,
    headers, retries and JSON handling can be tested; it reports a made-up model name."""

    def __init__(self, token="test-runtime-token", model="nvidia/test-model-1", fail_first=0, prose_first=0):
        self.token, self.model, self.fail_first, self.prose_first = token, model, fail_first, prose_first
        super().__init__(_NightshiftHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"
