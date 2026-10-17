from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import datetime, timedelta

import aiohttp

from .config import Config, ConfigError

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SproutError(RuntimeError):
    pass


class SproutApiError(SproutError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class SproutClient:
    """Minimal webhook client for the Sprout Track API (/api/hooks/v1).

    Writes are throttled to stay clear of the shared 30/min POST/PUT/DELETE
    rate limit, and 429 responses are retried with backoff.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession = aiohttp.ClientSession(timeout=_TIMEOUT)
        self._last_write = 0.0
        # Each API key has its own 30 writes/min bucket; round-robin across all
        # configured keys multiplies the sustained write rate.
        self._keys: list[str] = list(config.sprout_api_keys)
        self._key_cycle = itertools.cycle(self._keys)

    @property
    def _base(self) -> str:
        return f"{self.config.sprout_base_url}/api/hooks/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {next(self._key_cycle)}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _data(payload: dict) -> dict:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._session.get(f"{self._base}{path}", headers=self._headers(), params=params) as response:
            return await self._raise_for_response(response, "GET")

    async def _raise_for_response(self, response: aiohttp.ClientResponse, operation: str) -> dict:
        try:
            payload = await response.json(content_type=None)
        except aiohttp.ContentTypeError, ValueError:
            payload = {}
        if response.status < 400:
            if payload.get("success") is False:
                error = payload.get("error") or {}
                raise SproutApiError(response.status, error.get("code", "UNKNOWN"), error.get("message", str(payload)))
            return payload
        error = (payload.get("error") or {}) if isinstance(payload, dict) else {}
        raise SproutApiError(
            response.status, error.get("code", "HTTP_ERROR"), error.get("message", f"{operation} {response.status}")
        )

    async def list_babies(self) -> list[dict]:
        payload = await self._get("/babies")
        return self._data(payload).get("babies") or []

    async def resolve_baby_id(self) -> str:
        if self.config.sprout_baby_id:
            return self.config.sprout_baby_id
        babies = await self.list_babies()
        if not babies:
            raise ConfigError("Sprout Track returned no babies for this API key")
        baby_id = babies[0].get("id")
        if not baby_id:
            raise ConfigError("Sprout Track baby is missing an id")
        _LOGGER.info("Resolved Sprout Track baby %s (%s)", baby_id, babies[0].get("firstName"))
        return baby_id

    async def get_reference(self, baby_id: str, reference_type: str | None = None) -> dict:
        params = {"type": reference_type} if reference_type else None
        payload = await self._get(f"/babies/{baby_id}/reference", params=params)
        return self._data(payload)

    async def list_activities(self, baby_id: str, activity_type: str, since: datetime) -> list[dict]:
        activities: list[dict] = []
        cursor = since.astimezone(self.config.timezone)
        while True:
            params = {
                "type": activity_type,
                "limit": 50,
                "since": cursor.isoformat(),
            }
            payload = await self._get(f"/babies/{baby_id}/activities", params=params)
            data = self._data(payload)
            batch = data.get("activities") or []
            activities.extend(batch)
            if not batch or not data.get("hasMore"):
                break
            starts = [self._parse_time(item) for item in batch]
            starts = [start for start in starts if start is not None]
            if not starts:
                # Can't advance the cursor; bail out to avoid an infinite loop.
                break
            next_cursor = min(starts) + timedelta(milliseconds=1)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        _LOGGER.debug("list_activities type=%s since=%s -> %d rows", activity_type, since.isoformat(), len(activities))
        return activities

    @staticmethod
    def _parse_time(item: dict) -> datetime | None:
        raw = item.get("time")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    async def post_activity(self, baby_id: str, payload: dict) -> dict:
        return await self._write("POST", f"{self._base}/babies/{baby_id}/activities", payload)

    async def update_activity(self, baby_id: str, activity_id: str, payload: dict) -> dict:
        """Edit an existing activity record (PUT /babies/:babyId/activities/:activityId).

        ``payload`` must include ``type`` and only fields valid for that type;
        unmapped fields keep their current values.
        """
        return await self._write("PUT", f"{self._base}/babies/{baby_id}/activities/{activity_id}", payload)

    async def _write(self, method: str, url: str, payload: dict) -> dict:
        await self._throttle()
        for attempt in range(4):
            async with self._session.request(method, url, headers=self._headers(), json=payload) as response:
                if response.status == 429:
                    await self._wait_rate_limit(response, attempt)
                    continue
                result = await self._raise_for_response(response, method)
                _LOGGER.debug("%s activity ok: %s", method, payload)
                return result
        raise SproutError(f"{method} activity kept hitting the rate limit")

    async def warm_up(self, *, timeout: float = 120.0, interval: float = 2.0) -> bool:
        """Poll the app until it answers (Sablier on-demand containers 502 while cold).

        Only the app root is touched, so no API key is needed. Returns True once
        a response below 500 arrives, False if the timeout expires first.
        """
        url = f"{self.config.sprout_base_url}/"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status < 500:
                        _LOGGER.info("Sprout Track is warm (HTTP %s)", response.status)
                        return True
            except TimeoutError, aiohttp.ClientError:
                pass
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)

    async def _throttle(self) -> None:
        loop = asyncio.get_event_loop()
        elapsed = loop.time() - self._last_write
        delay = self.config.write_delay_seconds / max(1, len(self._keys)) - elapsed
        if delay > 0:
            _LOGGER.debug("Throttling Sprout Track write for %.2fs", delay)
            await asyncio.sleep(delay)
        self._last_write = loop.time()

    async def _wait_rate_limit(self, response: aiohttp.ClientResponse, attempt: int) -> None:
        import time as _time

        # X-RateLimit-Reset is a Unix epoch (seconds); compare against the
        # real clock, never the monotonic loop timer.
        wait = 5.0 * (attempt + 1)
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                remaining = float(reset) - _time.time()
                if remaining > 0:
                    wait = remaining
            except ValueError:
                pass
        wait = min(max(wait, 1.0), 120.0)
        _LOGGER.warning("Sprout Track rate limited; waiting %.1fs", wait)
        await asyncio.sleep(wait)

    async def close(self) -> None:
        await self._session.close()
