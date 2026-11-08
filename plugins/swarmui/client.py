"""SwarmUI access: JSON over HTTP for everything, and (optionally) the generation WebSocket for live progress.

Messages raised as SwarmError are safe to show the model. They name a setting or say what to check, and never contain the URL, the
token, the session id or any text SwarmUI sent (a ToolError is shown to the model without the untrusted wrapper).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx2

AUTH_PAUSE = 60.0
MODELS_TTL = 30.0
PARAMS_TTL = 300.0


class SwarmError(Exception):
    """A failure whose message is safe to show."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str
    timeout: float
    max_response_bytes: int
    job_timeout: float


@dataclass
class SessionInfo:
    session_id: str
    user_id: str = ""
    version: str = ""
    permissions: list[str] = field(default_factory=list)


def ssl_context(verify: bool, ca_pem: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check_base_url(url: str, what: str = "") -> str:
    u = url.strip()
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.hostname or "://" not in u:
        raise ValueError("must start with http:// or https:// and name the host, for example http://swarm.local:7801")
    if parts.username or parts.password or parts.query or parts.fragment or any(c.isspace() for c in u):
        raise ValueError("must not contain credentials, spaces, a query or a fragment")
    return u.rstrip("/")


def _tls(exc: BaseException | None) -> bool:
    n = 0
    while exc is not None and n < 6:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__
        n += 1
    return False


class Swarm:
    def __init__(self, http: httpx2.AsyncClient, get_settings: Callable[[], Settings], logger) -> None:
        self._http = http
        self._get = get_settings  # called per operation so the token is read at the moment of use
        self._log = logger
        self._session: SessionInfo | None = None
        self._blocked_until = 0.0
        self._models: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._params: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------- core
    def _blocked(self) -> None:
        if time.monotonic() < self._blocked_until:
            raise SwarmError("SwarmUI refused the token a moment ago. Check the auth_token setting (it may have been revoked or the account may need one). "
                             f"Calls are paused for {int(AUTH_PAUSE)} seconds.")

    def _refused(self) -> SwarmError:
        self._blocked_until = time.monotonic() + AUTH_PAUSE
        self._session = None
        return SwarmError("SwarmUI refused the token. Check the auth_token setting (the instance may need one, or it may have been revoked).")

    @staticmethod
    def _headers(s: Settings) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if s.token:
            h["Cookie"] = f"swarm_token={s.token}"
        return h

    async def _post(self, route: str, payload: dict[str, Any] | None = None, *, session: bool = True, long: bool = False, _retry: bool = True) -> dict[str, Any]:
        self._blocked()
        s: Settings = self._get()
        body = dict(payload or {})
        if session:
            body["session_id"] = (await self.session()).session_id
        timeout = httpx2.Timeout(s.job_timeout if long else s.timeout)
        try:
            async with self._http.stream("POST", f"{s.base_url}/API/{route}", content=json.dumps(body).encode(), headers=self._headers(s), timeout=timeout) as r:
                status = r.status_code
                if status in (401, 403):
                    raise self._refused()
                if status in (301, 302, 303, 307, 308):
                    raise SwarmError("SwarmUI redirected the request. Check the base_url setting (for example http versus https, or a missing port).")
                if status == 404:
                    raise SwarmError("SwarmUI does not have that route. Check the base_url setting and the SwarmUI version.")
                if status >= 400:
                    raise SwarmError(f"SwarmUI rejected the request (status {status}).")
                raw = bytearray()
                async for chunk in r.aiter_bytes():
                    raw += chunk
                    if len(raw) > s.max_response_bytes:
                        raise SwarmError("SwarmUI's reply is larger than the max_response_bytes setting allows.")
        except SwarmError:
            raise
        except httpx2.TimeoutException:
            raise SwarmError("SwarmUI did not answer in time. Check the base_url and timeout_seconds settings.") from None
        except httpx2.UnsupportedProtocol:
            raise SwarmError("The base_url setting is not usable: it must start with http:// or https://.") from None
        except httpx2.ConnectError as e:
            if _tls(e):
                raise SwarmError("SwarmUI's certificate was not accepted. Check base_url, or set ca_certificate for a private certificate authority.") from None
            raise SwarmError("Cannot connect to SwarmUI. Check the base_url setting and that SwarmUI is running and reachable from this host.") from None
        except httpx2.HTTPError as e:
            self._log.warning("swarmui request failed: %s", type(e).__name__)
            raise SwarmError("Cannot reach SwarmUI (the connection failed). Check the base_url setting.") from None
        try:
            data = json.loads(bytes(raw))
        except ValueError:
            raise SwarmError("SwarmUI returned something that is not JSON. Check that base_url points at SwarmUI, not a login or proxy page.") from None
        if not isinstance(data, dict):
            raise SwarmError("SwarmUI returned an unexpected reply.")
        if data.get("error_id") == "invalid_session_id" and session and _retry:
            self._session = None
            return await self._post(route, payload, session=session, long=long, _retry=False)
        if "error" in data or "error_id" in data:
            text = f"{data.get('error', '')} {data.get('error_id', '')}".lower()
            if data.get("error_id") == "invalid_session_id":
                raise SwarmError("SwarmUI would not keep a session open. Try again.")
            if "permission" in text or "not allowed" in text:
                raise SwarmError("The SwarmUI user behind this token may not do that (a permission is missing in its role).")
            if "login" in text or "token" in text or "auth" in text:
                raise self._refused()
            raise SwarmError("SwarmUI reported an error for that request.")  # its own text is not repeated
        return data

    async def session(self) -> SessionInfo:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            data = await self._post("GetNewSession", {}, session=False)
            sid = data.get("session_id")
            if not isinstance(sid, str) or not sid:
                raise SwarmError("SwarmUI did not give a session. Check the auth_token setting and the SwarmUI version.")
            perms = data.get("permissions")
            self._session = SessionInfo(sid, str(data.get("user_id") or ""), str(data.get("version") or ""),
                                        [p for p in perms if isinstance(p, str)] if isinstance(perms, list) else [])
            return self._session

    # ------------------------------------------------------------- read calls
    async def status(self) -> dict[str, Any]:
        return await self._post("GetCurrentStatus", {})

    async def models(self, subtype: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        hit = self._models.get(subtype)
        if hit and not fresh and time.monotonic() - hit[0] < MODELS_TTL:
            return hit[1]
        data = await self._post("ListModels", {"path": "", "depth": 8, "subtype": subtype, "sortBy": "Name"})
        files = [f for f in data.get("files", []) if isinstance(f, dict) and isinstance(f.get("name"), str)] if isinstance(data.get("files"), list) else []
        self._models[subtype] = (time.monotonic(), files)
        return files

    async def describe(self, name: str, subtype: str) -> dict[str, Any] | None:
        data = await self._post("DescribeModel", {"modelName": name, "subtype": subtype})
        model = data.get("model")
        return model if isinstance(model, dict) else None

    async def params(self) -> dict[str, dict[str, Any]]:
        if self._params and time.monotonic() - self._params[0] < PARAMS_TTL:
            return self._params[1]
        data = await self._post("ListT2IParams", {})
        out = {p["id"]: p for p in data.get("list", []) if isinstance(p, dict) and isinstance(p.get("id"), str)} if isinstance(data.get("list"), list) else {}
        self._params = (time.monotonic(), out)
        return out

    async def images(self, path: str, depth: int, text_filter: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": path, "depth": depth, "sortBy": "Date", "sortReverse": True}
        if text_filter:
            payload["filter"] = text_filter
        return await self._post("ListImages", payload)

    # -------------------------------------------------------------- generation
    async def generate_http(self, params: dict[str, Any]) -> list[str]:
        data = await self._post("GenerateText2Image", params, long=True)
        images = data.get("images")
        return [i for i in images if isinstance(i, str)] if isinstance(images, list) else []

    async def generate_ws(self, params: dict[str, Any], on_progress: Callable[[float], None]) -> list[str]:
        """Generate over the WebSocket, reporting progress. Needs wsproto in the host (httpx2's WebSocket client imports it)."""
        self._blocked()
        s: Settings = self._get()
        session = await self.session()
        url = ("wss://" if s.base_url.startswith("https://") else "ws://") + s.base_url.split("://", 1)[1] + "/API/GenerateText2ImageWS"
        headers = {"Cookie": f"swarm_token={s.token}"} if s.token else {}
        images: list[str] = []
        failed = False
        try:
            async with self._http.websocket(url, headers=headers, max_message_size_bytes=max(s.max_response_bytes, 8_000_000), keepalive_ping_interval_seconds=None) as ws:
                await ws.send_json({**params, "session_id": session.session_id})
                while True:
                    try:
                        msg = await ws.receive_json(timeout=s.job_timeout)
                    except (TimeoutError, asyncio.TimeoutError):
                        raise
                    except Exception as e:  # noqa: BLE001 - the client's disconnect exceptions end the stream
                        if type(e).__name__ in ("WebSocketDisconnect", "WebSocketNetworkError", "EndOfStream", "WebSocketInvalidTypeReceived"):
                            break
                        raise
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("error_id") == "invalid_session_id":
                        self._session = None
                        raise SwarmError("SwarmUI would not keep a session open. Try again.")
                    if "error" in msg:
                        failed = True
                        break
                    prog = msg.get("gen_progress")
                    if isinstance(prog, dict) and isinstance(prog.get("overall_percent"), (int, float)):
                        on_progress(max(0.0, min(1.0, float(prog["overall_percent"]))))
                    img = msg.get("image")
                    if isinstance(img, dict) and isinstance(img.get("image"), str):
                        images.append(img["image"])
        except Exception as e0:  # noqa: BLE001 - the WebSocket client raises groups of exceptions; classify the first real one
            e: BaseException = e0
            while isinstance(e, BaseExceptionGroup) and e.exceptions:
                e = e.exceptions[0]
            if isinstance(e, SwarmError):
                raise e from None
            if isinstance(e, ImportError):
                raise SwarmError("Live progress needs the wsproto library, which this host does not have. Turn off progress_updates.") from None
            if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                raise SwarmError("SwarmUI did not finish in time (the job_timeout_seconds setting).") from None
            self._log.warning("swarmui websocket failed: %s", type(e).__name__)
            if _tls(e):
                raise SwarmError("SwarmUI's certificate was not accepted for the WebSocket. Check ca_certificate.") from None
            raise SwarmError("The connection to SwarmUI failed during generation. Check the base_url setting.") from None
        if failed and not images:
            raise SwarmError("SwarmUI reported an error for that request.")
        return images

    async def interrupt(self) -> None:
        await self._post("InterruptAll", {"other_sessions": False})
