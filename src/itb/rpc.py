"""Minimal JSON-RPC client with retry/backoff and optional fallback endpoint."""
from __future__ import annotations

import time

import httpx


NO_RETRY_CODES = {-32004, -32007, -32009, -32602, -32015}  # block not yet available / skipped / bad params


class RpcError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class Rpc:
    def __init__(self, url: str, fallback: str | None = None, retries: int = 4, timeout: float = 30):
        self.urls = [u for u in (url, fallback) if u]
        self.retries = retries
        self.client = httpx.Client(timeout=timeout)

    def call(self, method: str, params: list | None = None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last: Exception | None = None
        for attempt in range(self.retries):
            for url in self.urls:
                try:
                    r = self.client.post(url, json=body)
                    r.raise_for_status()
                    j = r.json()
                    if "error" in j:
                        e = RpcError(f"{method}: {j['error']}", j["error"].get("code"))
                        raise e
                    return j["result"]
                except RpcError as e:
                    if e.code in NO_RETRY_CODES:
                        raise
                    last = e
                except httpx.HTTPError as e:
                    last = e
            time.sleep(min(2 ** attempt * 0.5, 8))
        raise RpcError(f"{method} failed after retries: {last}")
