"""New-mail notification: real host manager, real MCP client, fake IMAP over TLS and a fake Fire endpoint over TLS.

The plugin's HTTP client is handed a transport that moves the connection to the fake while the request still names api.anthropic.com,
so the tests see exactly what the plugin sent (URL, headers, body). No real service or credential is used.
"""

import asyncio
import contextlib
import json
import logging
import ssl

import httpx2
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from conftest import free_port, running_plugin
from fake_imap import ALLOWED, FakeImap, Msg
from test_email import APP_PASSWORD, call, mail, values

TRIG = "trig_01ABCDEFGHJKLMNOPQRSTUVW"
FIRE = f"https://api.anthropic.com/v1/claude_code/routines/{TRIG}/fire"
TOKEN = "sk-ant-oat01-NOTIFYTOKEN0123456789"
SUBJECT = "SECRET-SUBJECT-xyz"
SENDER = "Mallory <mallory@evil.example>"


class FakeFire:
    """The Fire endpoint: records every request, answers with a configurable status."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.redirect: str | None = None
        self.hits_elsewhere = 0
        self.app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["POST", "GET"])])

    async def _handle(self, request):
        if not request.url.path.startswith("/v1/claude_code/"):
            self.hits_elsewhere += 1
            return Response("elsewhere")
        self.requests.append({"path": request.url.path, "headers": dict(request.headers), "body": (await request.body()).decode()})
        if self.redirect:
            return Response(status_code=307, headers={"location": self.redirect})
        if self.status != 200:
            return Response(status_code=self.status)
        return JSONResponse({"type": "routine_fire", "claude_code_session_id": "session_x", "claude_code_session_url": "https://claude.ai/code/session_x"})

    def texts(self) -> list[dict]:
        return [json.loads(json.loads(r["body"])["text"]) for r in self.requests]


@contextlib.asynccontextmanager
async def serve_tls(fake: FakeFire, imap: FakeImap):
    port = free_port()
    cert, key = imap._dir / "c.pem", imap._dir / "k.pem"
    server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ssl_certfile=str(cert), ssl_keyfile=str(key)))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


class Redirecting(httpx2.AsyncHTTPTransport):
    """Sends the connection to the fake (with certificate checking against the fake's own certificate) and remembers what the plugin asked for."""

    def __init__(self, port: int, cert_pem: str) -> None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=cert_pem)
        super().__init__(verify=ctx)
        self.port = port
        self.asked: list[str] = []

    async def handle_async_request(self, request):
        self.asked.append(str(request.url))
        request.url = request.url.copy_with(host="127.0.0.1", port=self.port)
        return await super().handle_async_request(request)


@contextlib.asynccontextmanager
async def watched(imap, tmp_path, fire, **extra):
    """Run the plugin with notifications on, wait for the first check (the baseline), and yield (client, poller, transport)."""
    async with serve_tls(fire, imap) as port:
        cfg = values(imap, notify_url=FIRE, **extra)
        async with running_plugin(tmp_path, "email", cfg, {"password": APP_PASSWORD, "notify_token": TOKEN}) as (c, lp, _):
            poller = lp.plugin.poller
            transport = Redirecting(port, imap.cert_pem)
            lp.plugin.http._transport = transport  # the plugin's own client, with its own settings, now talks to the fake
            lp.plugin.poller.notifier.http = httpx2.AsyncClient(transport=transport, timeout=10, follow_redirects=False)
            # `ticks` counts checks that have *started*, so it is 1 while the baseline is still being taken; a test that then adds mail
            # races the baseline (mail added to a folder not yet looked at lands inside it and is never reported). Wait for the check
            # itself: it holds the poller's lock from its start to its end, so taking the lock waits for it to finish.
            async with asyncio.timeout(30):
                while poller.ticks < 1:
                    await asyncio.sleep(0.01)
                async with poller._lock:
                    pass
            assert poller.ticks == 1 and poller.last_result != "no check yet" and poller.state  # the baseline is complete, and no second check ran
            yield c, poller, transport


@pytest.fixture
async def imap():
    fake = FakeImap(password=APP_PASSWORD)
    fake.add("INBOX", "\\HasNoChildren", [Msg(1, mail("Old one")), Msg(2, mail("Old two"))])
    fake.add("Projects/work", "\\HasNoChildren", [Msg(10, mail("Work old"))])
    fake.add("[Gmail]/Spam", "\\HasNoChildren \\Junk", [Msg(20, mail("Spam old"))])
    fake.add("Private", "\\HasNoChildren", [])
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def fire():
    return FakeFire()


def add(imap, box, uid, subject=SUBJECT, sender=SENDER):
    imap.boxes[box]["msgs"].append(Msg(uid, mail(subject, "body text", sender=sender)))


async def test_off_by_default_and_status_says_so(imap, tmp_path):
    async with running_plugin(tmp_path, "email", values(imap), {"password": APP_PASSWORD}) as (c, lp, _):
        assert lp.plugin.poller is None and lp.plugin.http is None
        _, text = await call(c, "mailbox_status")
        assert "notifications are off" in text
        assert [n for n in imap.commands if n.startswith("EXAMINE")] == []  # no polling happened


async def test_first_check_only_sets_a_baseline_then_new_mail_is_reported_once(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, transport):
        assert fire.requests == []  # existing mail is never replayed
        add(imap, "INBOX", 3)
        add(imap, "Projects/work", 11)
        await poller.tick()
        assert len(fire.requests) == 1
        req = fire.requests[0]
        assert transport.asked == [FIRE] and req["path"] == f"/v1/claude_code/routines/{TRIG}/fire"  # the plugin named Anthropic's endpoint
        h = req["headers"]
        assert h["authorization"] == "Bearer " + TOKEN and h["anthropic-beta"] == "experimental-cc-routine-2026-04-01" and h["anthropic-version"] == "2023-06-01"
        assert fire.texts() == [{"event": "new_mail", "count": 2, "more_pending": 0, "folders": {"INBOX": [3], "Projects/work": [11]}}]
        await poller.tick()
        assert len(fire.requests) == 1  # nothing new, nothing sent
        assert poller.pending_count() == 0


async def test_payload_has_ids_only_never_mail_text(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        add(imap, "INBOX", 3)
        await poller.tick()
        body = fire.requests[0]["body"]
        for leak in (SUBJECT, "mallory", "evil.example", "body text"):
            assert leak not in body


async def test_changed_uidvalidity_resets_the_baseline_without_reporting(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        imap.validity = 888
        add(imap, "INBOX", 3)
        await poller.tick()
        assert fire.requests == []
        add(imap, "INBOX", 4)
        await poller.tick()
        assert fire.texts()[0]["folders"] == {"INBOX": [4]}


async def test_blocked_folders_are_never_opened_or_reported(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        add(imap, "[Gmail]/Spam", 21)
        add(imap, "INBOX", 3)
        await poller.tick()
        assert fire.texts()[0]["folders"] == {"INBOX": [3]}
        assert "[Gmail]/Spam" not in imap.examined and "Spam" not in "".join(imap.examined)


async def test_notify_folders_narrows_the_watch(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire, notify_folders=["projects/work"]) as (c, poller, _):
        add(imap, "INBOX", 3)
        add(imap, "Projects/work", 11)
        await poller.tick()
        assert fire.texts()[0]["folders"] == {"Projects/work": [11]}
        assert poller.watching == 1


async def test_failed_call_keeps_the_ids_and_retries(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        fire.status = 500
        add(imap, "INBOX", 3)
        await poller.tick()
        assert poller.pending_count() == 1 and "status 500" in poller.last_result
        fire.status = 200
        await poller.tick()
        assert poller.pending_count() == 0 and fire.texts()[-1]["folders"] == {"INBOX": [3]}
        _, text = await call(c, "mailbox_status")
        assert "New-mail notifications are on" in text and "0 waiting" in text


async def test_refused_token_pauses_sending_and_names_the_setting(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        fire.status = 401
        add(imap, "INBOX", 3)
        await poller.tick()
        assert "notify_token" in poller.last_result and TOKEN not in poller.last_result
        fire.status = 200
        before = len(fire.requests)
        await poller.tick()  # paused: no further calls with a token Anthropic refused
        assert len(fire.requests) == before and poller.pending_count() == 1
        poller.pause_until = 0  # the hour passes
        await poller.tick()
        assert poller.pending_count() == 0
        fire.status = 404
        add(imap, "INBOX", 4)
        await poller.tick()
        assert "notify_url" in poller.last_result


async def test_redirects_are_not_followed(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        fire.redirect = "https://api.anthropic.com/elsewhere"
        add(imap, "INBOX", 3)
        await poller.tick()
        assert len(fire.requests) == 1 and fire.hits_elsewhere == 0 and poller.pending_count() == 1


async def test_a_flood_is_sent_in_small_batches_within_the_hourly_cap(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        for uid in range(3, 3 + 45):
            add(imap, "INBOX", uid)
        await poller.tick()
        first = fire.texts()[0]
        assert first["count"] == 20 and first["more_pending"] == 25 and len(first["folders"]["INBOX"]) == 20
        await poller.tick()
        await poller.tick()
        assert [t["count"] for t in fire.texts()] == [20, 20, 5] and poller.pending_count() == 0
        # the hourly cap
        poller.fires.extend([poller._clock()] * (20 - len(poller.fires)))
        add(imap, "INBOX", 100)
        before = len(fire.requests)
        await poller.tick()
        assert len(fire.requests) == before and "this hour" in poller.last_result and poller.pending_count() == 1


async def test_only_two_hundred_ids_wait_and_the_oldest_go_first(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        fire.status = 500
        for uid in range(3, 3 + 150):
            add(imap, "INBOX", uid)
        await poller.tick()
        for uid in range(153, 153 + 150):
            add(imap, "INBOX", uid)
        await poller.tick()
        assert poller.pending_count() == 200 and poller.pending["INBOX"][0] == 103 and poller.pending["INBOX"][-1] == 302


async def test_a_server_that_does_not_announce_uidnext_still_reports_each_message_once(imap, tmp_path, fire):
    imap.announce_uidnext = False  # then "uid n:*" always returns the last message, even when it is old
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        assert fire.requests == []
        await poller.tick()
        assert fire.requests == []  # the old last message is not mistaken for new mail
        add(imap, "INBOX", 3)
        await poller.tick()
        await poller.tick()
        assert fire.texts() == [{"event": "new_mail", "count": 1, "more_pending": 0, "folders": {"INBOX": [3]}}]


async def test_secrets_stay_out_of_logs_results_and_audit(imap, tmp_path, fire, caplog):
    caplog.set_level(logging.DEBUG)
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        add(imap, "INBOX", 3)
        fire.status = 401
        await poller.tick()
        _, status = await call(c, "mailbox_status")
        blob = status + poller.last_result
    for secret in (TOKEN, TRIG, APP_PASSWORD):
        assert secret not in blob and secret not in caplog.text
    assert TOKEN not in (tmp_path / "audit.jsonl").read_text()


async def test_polling_is_read_only_and_stops_on_unload(imap, tmp_path, fire):
    async with watched(imap, tmp_path, fire) as (c, poller, _):
        add(imap, "INBOX", 3)
        await poller.tick()
        task = poller.task
        assert not task.done()
    assert task.done()
    assert {x.split()[0] for x in imap.commands} <= ALLOWED
    assert not any(x.startswith(("SELECT", "STORE", "COPY", "APPEND", "EXPUNGE")) for x in imap.commands)
    assert all(m.flags == () for m in imap.boxes["INBOX"]["msgs"])  # still unread


@pytest.mark.parametrize("url", [
    "http://api.anthropic.com/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://api.anthropic.com.evil.example/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://evil.example/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://api.anthropic.com@evil.example/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://api.anthropic.com:8443/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://127.0.0.1/v1/claude_code/routines/trig_01ABCDEFGH/fire",
    "https://api.anthropic.com/v1/claude_code/routines/trig_01ABCDEFGH/fire?x=1",
    "https://api.anthropic.com/v1/messages",
    "https://api.anthropic.com/v1/claude_code/routines/../../x/fire",
])
async def test_only_the_fire_endpoint_is_accepted(imap, tmp_path, url):
    from mcpopus.manager import PluginLoadError

    with pytest.raises(PluginLoadError) as e:
        async with running_plugin(tmp_path, "email", values(imap, notify_url=url), {"password": APP_PASSWORD, "notify_token": TOKEN}):
            pass
    assert "notify_url" in str(e.value) and "evil" not in str(e.value) and TOKEN not in str(e.value)


async def test_url_without_token_and_bad_token_are_refused(imap, tmp_path):
    from mcpopus.manager import PluginLoadError

    with pytest.raises(PluginLoadError) as e:
        async with running_plugin(tmp_path / "a", "email", values(imap, notify_url=FIRE), {"password": APP_PASSWORD}):
            pass
    assert "notify_token" in str(e.value)
    bad = "tok\r\nX-Evil: 1"
    with pytest.raises(PluginLoadError) as e:
        async with running_plugin(tmp_path / "b", "email", values(imap, notify_url=FIRE), {"password": APP_PASSWORD, "notify_token": bad}):
            pass
    assert "notify_token" in str(e.value) and "Evil" not in str(e.value)


async def test_poll_interval_is_bounded(imap, tmp_path):
    from mcpopus.manager import PluginLoadError

    for secs in (10, 119, 3601):
        with pytest.raises(PluginLoadError) as e:
            async with running_plugin(tmp_path / str(secs), "email", values(imap, poll_seconds=secs), {"password": APP_PASSWORD}):
                pass
        assert "poll_seconds" in str(e.value)
