"""One bounded, read-only OTGW transport per HA instance and endpoint.

No background task or control writer. Cached reads retain acquisition time;
failed reads never return an earlier successful snapshot.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from random import uniform
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .evidence import http_epoch_modulus

DATA_KEY = "sat_otgw_monitor_readers"
MIN_INTERVAL = 2.0
REQUEST_TIMEOUT = 5.0


class MonitorUnavailable(ValueError):
    pass


class Snapshot(dict):
    def __init__(self, data, acquired_at, epoch_modulus=None):
        super().__init__(deepcopy(data))
        self.acquired_at = acquired_at
        self.epoch_modulus = epoch_modulus


def retry_delay(header, body):
    values = [2.0]
    for raw in (header, body.get("retry_after") if isinstance(body, dict) else None):
        if raw is None or isinstance(raw, bool):
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            try:
                seconds = (parsedate_to_datetime(str(raw)) -
                           datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if isfinite(seconds) and seconds >= 0:
            values.append(seconds)
    # Honor the server's minimum and avoid repeated fixed-phase collisions.
    return max(values) + uniform(0.1, 1.0)


def shared_reader(hass, url):
    parts = urlsplit(url)
    host = parts.hostname
    host = f"[{host}]" if ":" in host else host
    port = parts.port
    if port and port != (443 if parts.scheme == "https" else 80):
        host = f"{host}:{port}"
    canonical = urlunsplit((parts.scheme.lower(), host.lower(), parts.path, "", ""))
    readers = hass.data.setdefault(DATA_KEY, {})
    if canonical not in readers:
        readers[canonical] = Reader(hass, canonical)
    return readers[canonical]


class Reader:
    def __init__(self, hass, url):
        self.hass, self.url = hass, url
        self.lock = asyncio.Lock()
        self.snapshot = None
        self.last_error = None
        self.next_request = 0.0
        self.epoch_modulus = None

    async def read(self):
        async with self.lock:
            now = monotonic()
            if now < self.next_request:
                if self.snapshot is not None:
                    return Snapshot(self.snapshot, self.snapshot.acquired_at, self.epoch_modulus)
                raise MonitorUnavailable("OTGW monitor backoff")
            self.snapshot = None
            # Set before awaiting: cancellation/failure cannot cause a retry burst.
            self.next_request = now + MIN_INTERVAL
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with async_get_clientsession(self.hass).get(
                            self.url, allow_redirects=False) as response:
                        if response.status == 429:
                            try:
                                body = await response.json()
                            except Exception:
                                body = {}
                            self.next_request = monotonic() + retry_delay(
                                response.headers.get("Retry-After"), body)
                            raise MonitorUnavailable("OTGW monitor HTTP 429")
                        if response.status != 200:
                            raise MonitorUnavailable(f"OTGW monitor HTTP {response.status}")
                        data = await response.json()
                        if not isinstance(data, dict) or not isinstance(data.get("otmonitor"), dict):
                            raise MonitorUnavailable("OTGW monitor schema")
                        acquired = monotonic()
                        self.epoch_modulus = http_epoch_modulus(data["otmonitor"], self.epoch_modulus)
                        self.snapshot = Snapshot(data["otmonitor"], acquired, self.epoch_modulus)
                        self.next_request = acquired + MIN_INTERVAL
                        return Snapshot(self.snapshot, acquired, self.epoch_modulus)
            except BaseException as exc:
                # Retain the cause across backoff; never include URLs/credentials.
                self.last_error = str(exc) if isinstance(exc, MonitorUnavailable) else type(exc).__name__
                self.snapshot = None
                self.next_request = max(self.next_request, monotonic() + MIN_INTERVAL)
                raise


def transient_read_error(exc):
    """Only temporary acquisition failures; schema/auth/session errors are not gaps."""
    return isinstance(exc, (TimeoutError, OSError)) or (
        isinstance(exc, MonitorUnavailable) and str(exc) in (
            'OTGW monitor backoff', 'OTGW monitor HTTP 429',
            'OTGW monitor HTTP 502', 'OTGW monitor HTTP 503', 'OTGW monitor HTTP 504'))
