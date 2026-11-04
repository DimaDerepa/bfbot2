"""Shared test helpers for store adapters.

Usage in a store test:

    from tests.conftest import FakeHttp, make_store

    def test_parse():
        http = FakeHttp({"emag.ro/search": EMAG_HTML})          # url substring -> body (str, dict/list for JSON)
        store = make_store(EmagStore, http)
        offers = store.search("iphone 16 pro")
        assert http.calls[0].startswith("https://www.emag.ro/search/iphone%2016%20pro")
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.net import BlockedError, FetchError, Response, looks_blocked  # noqa: E402


class FakeHttp:
    """Drop-in replacement for bot.net.Http: serves canned bodies by URL substring."""

    def __init__(self, routes: dict[str, Any], status: int = 200):
        self.routes = routes
        self.status = status
        self.calls: list[str] = []
        self.headers_sent: list[dict] = []
        self.requests_made = 0
        self.last_response = None

    def _full(self, url: str, params: dict | None) -> str:
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        return url

    def request(self, method: str, url: str, *, params=None, headers=None, data=None, json_body=None,
                allow_blocked=False) -> Response:
        full = self._full(url, params)
        self.calls.append(full)
        self.headers_sent.append(headers or {})
        self.requests_made += 1
        for key, body in self.routes.items():
            if key in full:
                status = self.status
                if isinstance(body, tuple):
                    status, body = body
                text = body if isinstance(body, str) else json.dumps(body)
                resp = Response(url=full, status=status, text=text, headers={})
                self.last_response = resp
                if not allow_blocked:
                    if looks_blocked(status, text):
                        raise BlockedError(f"blocked (HTTP {status})", status, full)
                    if status >= 400:
                        raise FetchError(f"HTTP {status}", status, full)
                return resp
        resp = Response(url=full, status=404, text="not found", headers={})
        self.last_response = resp
        raise FetchError("HTTP 404 (no fake route)", 404, full)

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw) -> Any:
        return self.get(url, **kw).json()

    def post_json(self, url: str, body: Any, **kw) -> Any:
        return self.request("POST", url, json_body=body, **kw).json()


def make_store(cls, http: FakeHttp, max_pages: int = 1):
    return cls(http, max_pages=max_pages)


@pytest.fixture
def fake_http():
    return FakeHttp
