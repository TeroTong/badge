from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


logger = logging.getLogger("smart_badge.hot_read_cache")


@dataclass
class _CachedHttpResponse:
    body: bytes
    status_code: int
    headers: dict[str, str]
    expires_at: float


class HotReadResponseCache:
    def __init__(self, *, max_items: int, max_body_bytes: int) -> None:
        self._max_items = max(1, max_items)
        self._max_body_bytes = max(1, max_body_bytes)
        self._items: OrderedDict[str, _CachedHttpResponse] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, key: str) -> _CachedHttpResponse | None:
        now = time.monotonic()
        async with self._guard:
            cached = self._items.get(key)
            if cached is None:
                return None
            if cached.expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return cached

    async def set(self, key: str, cached: _CachedHttpResponse) -> None:
        async with self._guard:
            self._items[key] = cached
            self._items.move_to_end(key)
            while len(self._items) > self._max_items:
                self._items.popitem(last=False)

    async def get_or_create(
        self,
        key: str,
        factory: Callable[[], Awaitable[Response]],
        *,
        ttl_seconds: float,
    ) -> Response:
        cached = await self.get(key)
        if cached is not None:
            return _response_from_cache(cached, hit=True)

        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock

        try:
            async with lock:
                try:
                    cached = await self.get(key)
                    if cached is not None:
                        return _response_from_cache(cached, hit=True)

                    response = await factory()
                    if response.status_code != 200:
                        return response

                    body = b""
                    too_large = False
                    async for chunk in response.body_iterator:
                        body += chunk
                        if len(body) > self._max_body_bytes:
                            too_large = True

                    if too_large:
                        return _clone_response(response, body, hit=False)

                    cached = _CachedHttpResponse(
                        body=body,
                        status_code=response.status_code,
                        headers=_cacheable_headers(response),
                        expires_at=time.monotonic() + max(0.1, ttl_seconds),
                    )
                    await self.set(key, cached)
                    return _response_from_cache(cached, hit=False)
                except Exception:
                    raise
        finally:
            async with self._guard:
                current_lock = self._locks.get(key)
                if current_lock is lock and not lock.locked():
                    self._locks.pop(key, None)


def _cacheable_headers(response: Response) -> dict[str, str]:
    excluded = {"content-length", "transfer-encoding"}
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in excluded
    }


def _response_from_cache(cached: _CachedHttpResponse, *, hit: bool) -> Response:
    headers = dict(cached.headers)
    headers["x-badge-hot-cache"] = "hit" if hit else "store"
    return Response(
        content=cached.body,
        status_code=cached.status_code,
        headers=headers,
    )


def _clone_response(response: Response, body: bytes, *, hit: bool) -> Response:
    headers = _cacheable_headers(response)
    headers["x-badge-hot-cache"] = "skip" if not hit else "hit"
    return Response(content=body, status_code=response.status_code, headers=headers)


def _auth_cache_fingerprint(request: Request) -> str:
    identity = "|".join(
        [
            request.headers.get("authorization", ""),
            request.headers.get("cookie", ""),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


class HotReadCacheMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        api_prefix: str,
        enabled: bool,
        ttl_seconds: float,
        badge_ttl_seconds: float,
        max_items: int,
        max_body_bytes: int,
    ) -> None:
        super().__init__(app)
        self.enabled = enabled
        self.api_prefix = api_prefix.rstrip("/")
        self.default_ttl_seconds = ttl_seconds
        self.badge_ttl_seconds = badge_ttl_seconds
        self.cache = HotReadResponseCache(max_items=max_items, max_body_bytes=max_body_bytes)

    async def dispatch(self, request: Request, call_next):
        ttl = self._ttl_for_request(request)
        if ttl is None:
            return await call_next(request)

        cache_key = self._cache_key(request)
        try:
            return await self.cache.get_or_create(cache_key, lambda: call_next(request), ttl_seconds=ttl)
        except Exception:
            logger.exception("hot read cache failed path=%s", request.url.path)
            raise

    def _ttl_for_request(self, request: Request) -> float | None:
        if not self.enabled or request.method.upper() != "GET":
            return None
        if request.headers.get("cache-control", "").lower().find("no-cache") >= 0:
            return None
        if not request.headers.get("authorization") and not request.headers.get("cookie"):
            return None

        path = request.url.path.rstrip("/")
        query = request.query_params
        hot_list_paths = {
            f"{self.api_prefix}/customers",
            f"{self.api_prefix}/recordings",
            f"{self.api_prefix}/visits",
            f"{self.api_prefix}/visit-orders",
            f"{self.api_prefix}/transcripts",
            f"{self.api_prefix}/staff",
            f"{self.api_prefix}/positions",
            f"{self.api_prefix}/sap-hana-visit-orders",
            f"{self.api_prefix}/hotwords/groups",
            f"{self.api_prefix}/rule-groups",
            f"{self.api_prefix}/risk-rules",
            f"{self.api_prefix}/quality/dimensions",
            f"{self.api_prefix}/analysis/results",
            f"{self.api_prefix}/sap-push-monitoring/logs",
        }
        if path in hot_list_paths:
            return self.default_ttl_seconds

        if path in {
            f"{self.api_prefix}/sap-push-monitoring/overview",
            f"{self.api_prefix}/asr-monitoring/overview",
        }:
            return max(self.default_ttl_seconds, 30.0)

        if path == f"{self.api_prefix}/dashboard" and query.get("detail_level", "summary") == "summary":
            return max(self.default_ttl_seconds, 10.0)

        if path in {f"{self.api_prefix}/account/managed-badges", f"{self.api_prefix}/account/my-badge"}:
            return self.badge_ttl_seconds

        if path == f"{self.api_prefix}/dingtalk/devices":
            return max(self.default_ttl_seconds, 10.0)

        parts = path[len(self.api_prefix):].strip("/").split("/") if path.startswith(self.api_prefix) else []
        if len(parts) == 2 and parts[0] in {"customers", "staff"}:
            return self.default_ttl_seconds
        if len(parts) == 3 and parts[0] == "customers" and parts[2] in {"detail", "merged-analysis", "tag-completion", "visit-orders"}:
            return self.default_ttl_seconds
        if len(parts) == 3 and parts[0] == "recordings" and parts[1] == "archive":
            return self.default_ttl_seconds

        return None

    def _cache_key(self, request: Request) -> str:
        return "|".join(
            [
                request.method.upper(),
                request.url.path.rstrip("/"),
                request.url.query,
                _auth_cache_fingerprint(request),
            ]
        )
