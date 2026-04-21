"""Minimal HTTP client over urllib.request.

Provides JSON decoding, bearer-style auth via caller-supplied headers, retry on
429 and 5xx (honouring ``Retry-After``), and two pagination helpers:

* ``paginate``     — GitLab-style ``page=N`` / ``X-Next-Page`` pagination.
* ``paginate_link`` — GitHub-style RFC 5988 ``Link`` header with ``rel="next"``.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from .. import log

_logger = log.get("git_sync.http")

_LINK_NEXT_RE = re.compile(r"<([^>]+)>\s*;\s*[^,]*?rel=\"next\"")


class HTTPError(Exception):
    def __init__(self, status: int, method: str, url: str, body: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:200]}")
        self.status = status
        self.method = method
        self.url = url
        self.body = body


class HTTPClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        return self._request("GET", self._build_url(path, params))

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, dict[str, str]]:
        data, extra = _encode_json(json_body)
        return self._request(
            "POST", self._build_url(path, params), data=data, extra_headers=extra,
        )

    def patch(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, dict[str, str]]:
        data, extra = _encode_json(json_body)
        return self._request(
            "PATCH", self._build_url(path, params), data=data, extra_headers=extra,
        )

    def put(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, dict[str, str]]:
        data, extra = _encode_json(json_body)
        return self._request(
            "PUT", self._build_url(path, params), data=data, extra_headers=extra,
        )

    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
    ) -> Iterator[Any]:
        merged: dict[str, Any] = dict(params or {})
        merged["per_page"] = per_page
        page = 1
        while True:
            merged["page"] = page
            body, headers = self._request("GET", self._build_url(path, merged))
            if not body:
                return
            yield from body
            next_page = headers.get("x-next-page", "").strip()
            if not next_page:
                return
            page = int(next_page)

    def paginate_link(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        per_page: int = 100,
    ) -> Iterator[Any]:
        merged: dict[str, Any] = dict(params or {})
        merged["per_page"] = per_page
        url = self._build_url(path, merged)
        while True:
            body, headers = self._request("GET", url)
            if not body:
                return
            yield from body
            next_url = _parse_link_next(headers.get("link", ""))
            if not next_url:
                return
            url = next_url

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        headers = {**self.headers, **(extra_headers or {})}

        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(url, method=method, data=data)
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    body: Any = json.loads(raw) if raw else None
                    return body, resp_headers
            except urllib.error.HTTPError as e:
                err_body = _read_error_body(e)
                if e.code == 429 or 500 <= e.code < 600:
                    if attempt < self.max_retries:
                        wait = _retry_delay(e, attempt, self.backoff_base)
                        _logger.warning(
                            "HTTP %d on %s %s; retrying in %.2fs (attempt %d/%d)",
                            e.code, method, url, wait, attempt + 1, self.max_retries,
                        )
                        self._sleep(wait)
                        continue
                raise HTTPError(e.code, method, url, err_body) from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    wait = self.backoff_base * (2 ** attempt) + random.uniform(0, 0.25)
                    _logger.warning(
                        "%s %s failed (%s); retrying in %.2fs",
                        method, url, e, wait,
                    )
                    self._sleep(wait)
                    continue
                raise

        raise RuntimeError("unreachable: retry loop exited without return")

    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        base = path if path.startswith("http") else f"{self.base_url}{path}"
        if not params:
            return base
        filtered = {k: v for k, v in params.items() if v is not None}
        return f"{base}?{urllib.parse.urlencode(filtered, doseq=True)}"


def _encode_json(json_body: Any) -> tuple[bytes | None, dict[str, str]]:
    if json_body is None:
        return None, {}
    return json.dumps(json_body).encode(), {"Content-Type": "application/json"}


def _read_error_body(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return ""


def _retry_delay(err: urllib.error.HTTPError, attempt: int, base: float) -> float:
    retry_after = err.headers.get("Retry-After") if err.headers else None
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base * (2 ** attempt) + random.uniform(0, 0.25)


def _parse_link_next(header: str) -> str | None:
    if not header:
        return None
    match = _LINK_NEXT_RE.search(header)
    return match.group(1) if match else None
