"""New-mail notification: poll the visible folders and tell a Claude Code scheduled task (routine) that mail arrived.

Only folder names and numeric message ids leave this module: never a sender, subject, date or body, because those are written by
other people. The destination is fixed to Anthropic's Fire endpoint by `check_fire_url`. State lives in memory only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque

import httpx2

from .imapbackend import MailError

FIRE_URL = re.compile(r"https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]{8,64}/fire")
BETA = "experimental-cc-routine-2026-04-01"  # the dated beta header the endpoint ships under (research preview)
VERSION = "2023-06-01"
MAX_IDS_PER_FIRE = 20
MAX_PENDING = 200
MAX_FIRES_PER_HOUR = 20  # the documented limit is 30 an hour per task
AUTH_PAUSE = 3600.0
SEND_TIMEOUT = 10.0


def check_fire_url(url: str) -> str:
    if not FIRE_URL.fullmatch(url):
        raise ValueError("must be the Fire URL of a scheduled task, https://api.anthropic.com/v1/claude_code/routines/trig_.../fire")
    return url


class HideFireUrl(logging.Filter):
    """httpx2 logs every request URL at INFO, and the Fire URL names the scheduled task. Drop just those records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "/claude_code/routines/" not in record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot be formatted is dropped rather than risk showing it
            return False


_FILTER = HideFireUrl()
_users = 0  # a reload starts the new generation before the old one stops, so the filter is counted, not toggled


def hide_fire_url(on: bool) -> None:
    global _users
    logger = logging.getLogger("httpx2")
    _users = max(0, _users + (1 if on else -1))
    if _users and _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
    elif not _users:
        logger.removeFilter(_FILTER)


class NotifyError(Exception):
    """A failure whose message is safe to show: names a setting, never the URL, the token or server text."""


def build_text(ids: dict[str, list[int]], more: int) -> str:
    """Folder names (set by the owner) and numeric ids only."""
    count = sum(len(v) for v in ids.values())
    return json.dumps({"event": "new_mail", "count": count, "more_pending": more, "folders": ids}, ensure_ascii=True, separators=(",", ":"))


class Notifier:
    def __init__(self, get_url, get_token, http: httpx2.AsyncClient) -> None:
        self._url, self._token, self.http = get_url, get_token, http  # read at the moment of use

    async def fire(self, text: str) -> None:
        headers = {"Authorization": "Bearer " + self._token(), "anthropic-beta": BETA, "anthropic-version": VERSION, "Content-Type": "application/json"}
        try:
            r = await self.http.post(self._url(), headers=headers, content=json.dumps({"text": text}), timeout=SEND_TIMEOUT, follow_redirects=False)
        except httpx2.TimeoutException:
            raise NotifyError("The notification call timed out.") from None
        except (httpx2.TransportError, httpx2.InvalidURL, httpx2.UnsupportedProtocol):
            raise NotifyError("The notification call could not reach Anthropic. Check that the host can make outbound HTTPS connections.") from None
        if r.status_code in (401, 403):
            raise NotifyError("Anthropic refused the notification. Check the notify_token setting (the token may have been rotated).")
        if r.status_code == 404:
            raise NotifyError("Anthropic does not know that scheduled task. Check the notify_url setting.")
        if r.status_code == 429:
            raise NotifyError("Anthropic is rate limiting the notifications.")
        if not 200 <= r.status_code < 300:
            raise NotifyError(f"Anthropic answered the notification with status {r.status_code}.")


class Poller:
    """Remembers where each folder was, finds new mail, and passes ids on. One tick at a time."""

    def __init__(self, backend, notifier: Notifier, get_folders, interval: float, logger, clock=time.monotonic) -> None:
        self.backend, self.notifier, self._folders, self.interval, self._log, self._clock = backend, notifier, get_folders, interval, logger, clock
        self.state: dict[str, tuple[int, int]] = {}
        self.pending: dict[str, list[int]] = {}
        self.fires: deque[float] = deque()
        self.pause_until = 0.0
        self.ticks = 0
        self.watching = 0
        self.last_result = "no check yet"
        self.task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        hide_fire_url(True)
        self.task = asyncio.create_task(self._loop(), name="email-poll")

    async def stop(self) -> None:
        hide_fire_url(False)
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the loop must survive anything; logged by class only
                self.last_result = "failed (unexpected error)"
                self._log.warning("mail poll failed: %s", type(e).__name__)
            await asyncio.sleep(self.interval)

    def pending_count(self) -> int:
        return sum(len(v) for v in self.pending.values())

    def _remember(self, found: dict[str, list[int]]) -> None:
        for folder, uids in found.items():
            have = self.pending.setdefault(folder, [])
            have.extend(u for u in uids if u not in have)
        dropped = 0
        while self.pending_count() > MAX_PENDING:  # drop the oldest ids first
            folder = min((f for f, v in self.pending.items() if v), key=lambda f: self.pending[f][0])
            self.pending[folder].pop(0)
            dropped += 1
        if dropped:
            self._log.warning("mail poll dropped %d old notifications (more than %d waiting)", dropped, MAX_PENDING)
        self.pending = {f: v for f, v in self.pending.items() if v}

    async def tick(self) -> None:
        async with self._lock:
            self.ticks += 1
            try:
                found, self.state, self.watching = await self.backend.poll(self.state, tuple(self._folders()))
            except MailError as e:
                self.last_result = f"mail check failed: {e}"
                return
            self._remember(found)
            await self._deliver()

    async def _deliver(self) -> None:
        if not self.pending:
            self.last_result = "ok, nothing new waiting"
            return
        now = self._clock()
        if now < self.pause_until:
            self.last_result = "paused after Anthropic refused the token; check notify_token"
            return
        while self.fires and now - self.fires[0] > 3600:
            self.fires.popleft()
        if len(self.fires) >= MAX_FIRES_PER_HOUR:
            self.last_result = f"waiting: {MAX_FIRES_PER_HOUR} notifications already sent this hour"
            return
        send: dict[str, list[int]] = {}
        room = MAX_IDS_PER_FIRE
        for folder in sorted(self.pending, key=lambda f: self.pending[f][0]):
            take = self.pending[folder][:room]
            send[folder] = take
            room -= len(take)
            if room <= 0:
                break
        more = self.pending_count() - sum(len(v) for v in send.values())
        try:
            await self.notifier.fire(build_text(send, more))
        except NotifyError as e:
            self.last_result = f"notification failed: {e}"
            if "notify_token" in str(e):
                self.pause_until = now + AUTH_PAUSE
            return
        self.fires.append(now)
        for folder, taken in send.items():
            self.pending[folder] = self.pending[folder][len(taken):]
        self.pending = {f: v for f, v in self.pending.items() if v}
        self.last_result = f"ok, told Claude about {sum(len(v) for v in send.values())} new messages"
