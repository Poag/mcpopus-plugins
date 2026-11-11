"""Home Assistant access: REST for states, history, logbook and services; WebSocket for the area, device and entity registries.

Why two transports (design doc, section 2): the REST API has no registry endpoints, and the only REST way to learn an entity's area
is the template endpoint, which this plugin never uses. The registry lists are read-only WebSocket commands. The only WebSocket write is
create_helper (one session: create an input_boolean and put it in a category); everything else that changes anything is a REST call.

Messages raised as HAError are safe to show the model: they name a setting, never the URL, the token or anything Home Assistant said.
"""

from __future__ import annotations

import asyncio
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

STATES_TTL = 3.0
REGISTRY_TTL = 60.0
REGISTRY_RETRY = 20.0
SERVICES_TTL = 30.0
AUTH_PAUSE = 60.0
HELPER_WAIT = 3.0  # how long create_helper waits for the new entity to appear in the registry


class HAError(Exception):
    """A failure whose message is safe to show."""


class HelperRefused(HAError):
    """create_helper stopped before creating anything (for example the category does not exist)."""


class RegistryUnavailable(HAError):
    """Area and device data could not be read (the rest of the plugin still works)."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str
    timeout: float
    max_response_bytes: int


@dataclass
class Registry:
    areas: dict[str, str] = field(default_factory=dict)  # area id -> name
    entity_area: dict[str, str] = field(default_factory=dict)  # entity id -> area id (the entity's own, else its device's)
    categories: dict[str, str] = field(default_factory=dict)  # automation category id -> name
    entity_category: dict[str, str] = field(default_factory=dict)  # automation entity id -> category id
    helper_categories: dict[str, str] = field(default_factory=dict)  # helper category id -> name
    categories_known: bool = False  # likewise for the automation categories
    helper_categories_known: bool = False  # False when Home Assistant would not list them (so "none" is not the same as "could not check")

    def area_name(self, entity_id: str) -> str:
        return self.areas.get(self.entity_area.get(entity_id, ""), "")

    def automation_category(self, entity_id: str) -> str:
        return self.categories.get(self.entity_category.get(entity_id, ""), "")


def ssl_context(verify: bool, ca_pem: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def check_base_url(url: str) -> str:
    """Validate the configured base URL; raises ValueError with a message that does not echo it."""
    u = url.strip()
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or not parts.hostname or "://" not in u:
        raise ValueError("must start with http:// or https:// and name the host, for example http://homeassistant.local:8123")
    if parts.username or parts.password or parts.query or parts.fragment or any(c.isspace() for c in u):
        raise ValueError("must not contain credentials, spaces, a query or a fragment")
    return u.rstrip("/")


def _cause_is_tls(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 6:
        if isinstance(exc, ssl.SSLError):
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
        seen += 1
    return False


class HomeAssistant:
    def __init__(self, http: httpx2.AsyncClient, get_settings, logger) -> None:
        self._http = http
        self._get = get_settings  # called per operation, so the token is read at the moment of use
        self._log = logger
        self._blocked_until = 0.0
        self._admin_blocked_until = 0.0
        self._states: tuple[float, list[dict[str, Any]]] | None = None
        self._registry: tuple[float, Registry] | None = None
        self._registry_failed: tuple[float, RegistryUnavailable] | None = None
        self._services: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._reg_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ REST
    def _blocked(self) -> None:
        if time.monotonic() < self._blocked_until:
            raise HAError("Home Assistant refused the access token a moment ago. Check the token setting (it may have been revoked or mistyped). "
                          f"Calls are paused for {int(AUTH_PAUSE)} seconds so that repeated failures do not get this host banned.")

    def _refused(self) -> HAError:
        self._blocked_until = time.monotonic() + AUTH_PAUSE
        return HAError("Home Assistant refused the access token. Check the token setting (it may have been revoked or mistyped).")

    def _admin_refused(self) -> HAError:
        self._admin_blocked_until = time.monotonic() + AUTH_PAUSE
        return HAError("Home Assistant refused the automation config call. The automation definition tools need the token's user to be an administrator "
                       "(Home Assistant's automation config API is administrator-only), or the token may have been revoked. Everything else still works. "
                       f"These calls are paused for {int(AUTH_PAUSE)} seconds so that repeated failures do not get this host banned.")

    def _helper_refused(self) -> HAError:
        self._admin_blocked_until = time.monotonic() + AUTH_PAUSE
        return HAError("Home Assistant refused the helper command. Creating a helper and putting it in a category need the token's user to be an administrator, "
                       "or the token may have been revoked. Everything else still works. "
                       f"These calls are paused for {int(AUTH_PAUSE)} seconds so that repeated failures do not get this host banned.")

    async def _get_json(self, path: str, params: dict[str, str] | None = None, *, missing_ok: bool = False) -> Any:
        return await self._request("GET", path, params, missing_ok=missing_ok)

    async def _request(self, method: str, path: str, params: dict[str, str] | None = None, *, body: Any = None, missing_ok: bool = False,
                       admin: bool = False, bad_request: str = "") -> Any:
        write = method != "GET"
        s: Settings = self._get()
        if admin:  # the config API: refused (and paused) separately, so a token whose user is not an administrator costs only these tools
            if time.monotonic() < self._admin_blocked_until:
                raise self._admin_refused()
        else:
            self._blocked()
        token = s.token
        try:
            async with self._http.stream(method, s.base_url + path, params=params, json=body, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as r:
                status = r.status_code
                if status in (401, 403):
                    if admin:
                        raise self._admin_refused()
                    raise self._refused() if status == 401 else HAError("Home Assistant refused permission for this request. The token's user may not be allowed to read it.")
                if status == 404 and missing_ok:
                    return None
                if status in (301, 302, 303, 307, 308):
                    raise HAError("Home Assistant redirected the request. Check the base_url setting (for example http versus https, or a missing port).")
                if status == 404:
                    raise HAError("Home Assistant does not have that endpoint. Check the base_url setting and that the api (or frontend) integration is enabled.")
                if status == 400 and bad_request:
                    raise HAError(bad_request)
                if status == 400 and write:
                    raise HAError("Home Assistant rejected the call. Check that the service accepts that entity and that the data fields are right for it.")
                if status >= 500 and write:
                    raise HAError("Home Assistant reported an error while carrying out the call. Check the entity's state before trying again.")
                if status >= 400:
                    raise HAError(f"Home Assistant rejected the request (status {status}).")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > s.max_response_bytes:
                        raise HAError("Home Assistant's reply is larger than the max_response_bytes setting allows. Raise it, or narrow the request.")
        except HAError:
            raise
        except httpx2.TimeoutException:
            if write:
                raise HAError("Home Assistant did not answer in time, so the action may or may not have happened. Check the entity's state before trying again.") from None
            raise HAError("Home Assistant did not answer in time. Check the base_url and timeout_seconds settings.") from None
        except httpx2.UnsupportedProtocol:
            raise HAError("The base_url setting is not usable: it must start with http:// or https://.") from None
        except httpx2.ConnectError as e:
            if _cause_is_tls(e):
                raise HAError("Home Assistant's certificate was not accepted. Check base_url, or set ca_certificate for a private certificate authority.") from None
            raise HAError("Cannot connect to Home Assistant. Check the base_url setting and that Home Assistant is running and reachable from this host.") from None
        except httpx2.HTTPError as e:
            self._log.warning("home assistant request failed: %s", type(e).__name__)
            raise HAError("Cannot reach Home Assistant (the connection failed). Check the base_url setting.") from None
        try:
            import json

            return json.loads(bytes(body))
        except ValueError:
            raise HAError("Home Assistant returned something that is not JSON. Check that base_url points at Home Assistant, not a login or proxy page.") from None

    async def info(self) -> dict[str, Any]:
        data = await self._get_json("/api/config")
        return data if isinstance(data, dict) else {}

    async def states(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not fresh and self._states and now - self._states[0] < STATES_TTL:
            return self._states[1]
        data = await self._get_json("/api/states")
        if not isinstance(data, list):
            raise HAError("Home Assistant returned an unexpected states reply.")
        states = [d for d in data if isinstance(d, dict) and isinstance(d.get("entity_id"), str)]
        self._states = (now, states)
        return states

    async def history(self, entity_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        data = await self._get_json(f"/api/history/period/{quote(start_iso, safe='')}", {
            "filter_entity_id": entity_id, "end_time": end_iso, "minimal_response": "", "no_attributes": ""})
        if not isinstance(data, list):
            raise HAError("Home Assistant returned an unexpected history reply.")
        rows: list[dict[str, Any]] = []
        for series in data[:1]:  # one entity was asked for
            if isinstance(series, list):
                rows += [r for r in series if isinstance(r, dict)]
        return rows

    async def logbook(self, entity_id: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        data = await self._get_json(f"/api/logbook/{quote(start_iso, safe='')}", {"entity": entity_id, "end_time": end_iso})
        if not isinstance(data, list):
            raise HAError("Home Assistant returned an unexpected logbook reply.")
        return [d for d in data if isinstance(d, dict)]

    async def state(self, entity_id: str) -> dict[str, Any] | None:
        """One entity's state straight from Home Assistant (no cache), or None when it does not exist."""
        data = await self._request("GET", f"/api/states/{quote(entity_id, safe='')}", missing_ok=True)
        return data if isinstance(data, dict) else None

    async def call_service(self, domain: str, service: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """POST one service call. The caller has already validated domain, service and body against the policy."""
        data = await self._request("POST", f"/api/services/{quote(domain, safe='')}/{quote(service, safe='')}", body=body)
        self._states = None  # the cached entity list is now stale
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def automation_config(self, config_id: str) -> dict[str, Any] | None:
        """The stored config of one automation (administrator-only in Home Assistant), or None when there is none."""
        data = await self._request("GET", f"/api/config/automation/config/{quote(config_id, safe='')}", missing_ok=True, admin=True)
        return data if isinstance(data, dict) else None

    async def save_automation_config(self, config_id: str, config: dict[str, Any]) -> None:
        await self._request("POST", f"/api/config/automation/config/{quote(config_id, safe='')}", body=config, admin=True,
                            bad_request="Home Assistant rejected the automation as invalid. Check its triggers, conditions and actions.")
        self._states = None

    async def delete_automation_config(self, config_id: str) -> None:
        await self._request("DELETE", f"/api/config/automation/config/{quote(config_id, safe='')}", admin=True,
                            bad_request="Home Assistant could not remove that automation (it may no longer exist).")
        self._states = None

    async def services(self) -> dict[str, dict[str, Any]]:
        """domain -> service -> description. Tolerates both documented shapes (a list of names, or a mapping)."""
        if self._services and time.monotonic() - self._services[0] < SERVICES_TTL:
            return self._services[1]
        data = await self._get_json("/api/services")
        out: dict[str, dict[str, Any]] = {}
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("domain"), str):
                continue
            svc = item.get("services")
            if isinstance(svc, dict):
                out[item["domain"]] = {k: (v if isinstance(v, dict) else {}) for k, v in svc.items() if isinstance(k, str)}
            elif isinstance(svc, list):
                out[item["domain"]] = {k: {} for k in svc if isinstance(k, str)}
        self._services = (time.monotonic(), out)
        return out

    # --------------------------------------------------------------- helpers
    async def create_helper(self, name: str, initial: bool, category_name: str) -> dict[str, Any]:
        """Create one input_boolean and put it in a helper category, in one WebSocket session. The caller has validated every argument against the policy.

        A helpers-scope category with the owner's configured name is created first if there is none (nothing else is ever created or changed in the category registry).
        Afterwards the result says whether the entity appeared in the registry and whether the category was assigned."""
        self._blocked()
        if time.monotonic() < self._admin_blocked_until:
            raise self._helper_refused()
        s: Settings = self._get()
        ws_url = ("wss://" if s.base_url.startswith("https://") else "ws://") + s.base_url.split("://", 1)[1] + "/api/websocket"
        counter = 0
        create_sent = False
        out: dict[str, Any] = {"item_id": "", "entity_id": "", "registered": False, "categorised": False, "category_created": False}

        async def command(ws, payload: dict[str, Any]) -> tuple[bool, Any, str]:
            nonlocal counter
            counter += 1
            await ws.send_json({"id": counter, **payload})
            while True:
                msg = await ws.receive_json(timeout=s.timeout)
                if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("id") == counter:
                    err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
                    return bool(msg.get("success")), msg.get("result"), str(err.get("code", ""))

        try:
            async with asyncio.timeout(s.timeout * 5 + HELPER_WAIT):
                async with self._http.websocket(ws_url, max_message_size_bytes=s.max_response_bytes, keepalive_ping_interval_seconds=None) as ws:
                    hello = await ws.receive_json(timeout=s.timeout)
                    if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                        raise HAError("Home Assistant's WebSocket did not start as expected. Check the base_url setting.")
                    await ws.send_json({"type": "auth", "access_token": s.token})
                    reply = await ws.receive_json(timeout=s.timeout)
                    if not isinstance(reply, dict) or reply.get("type") == "auth_invalid":
                        raise self._refused()
                    if reply.get("type") != "auth_ok":
                        raise HAError("Home Assistant's WebSocket did not accept the connection.")
                    ok, cats, _ = await command(ws, {"type": "config/category_registry/list", "scope": "helpers"})
                    if not ok or not isinstance(cats, list):
                        raise HelperRefused("Home Assistant would not list its helper categories, so nothing was created. This version of Home Assistant may not have helper categories.")
                    wanted = category_name.casefold()
                    cat_id = next((c["category_id"] for c in cats if isinstance(c, dict) and isinstance(c.get("category_id"), str)
                                   and str(c.get("name") or "").casefold() == wanted), None)
                    create_sent = True  # from here on something may have been written
                    if cat_id is None:  # the owner named it in claude_category: make it, in the helpers scope only
                        ok, cat, code = await command(ws, {"type": "config/category_registry/create", "scope": "helpers", "name": category_name})
                        if not ok:
                            if code in ("unauthorized", "forbidden"):
                                raise self._helper_refused()
                            raise HelperRefused("No helper category with the name in the claude_category setting exists, and Home Assistant would not create one, so nothing was created. "
                                                "Create it on the Helpers page (Settings, Devices and services, Helpers, then the Categories filter); "
                                                "a category of the same name for automations is a different one and does not count. Run status to see which of the two exist.")
                        cat_id = cat.get("category_id") if isinstance(cat, dict) else None
                        if not isinstance(cat_id, str) or not cat_id:
                            raise HAError("Home Assistant created the category but its reply was not understood, so no helper was created. Run status, then try again.")
                        out["category_created"] = True
                    ok, item, code = await command(ws, {"type": "input_boolean/create", "name": name, "initial": initial})
                    if not ok:
                        if code in ("unauthorized", "forbidden"):
                            raise self._helper_refused()
                        raise HAError("Home Assistant would not create the helper (it rejected the name or the fields). Nothing was created.")
                    item_id = item.get("id") if isinstance(item, dict) else None
                    if not isinstance(item_id, str) or not re.fullmatch(r"[a-z0-9_]{1,128}", item_id):
                        raise HAError("Home Assistant created something but its reply was not understood. Check Home Assistant's helpers before trying again.")
                    out["item_id"], out["entity_id"] = item_id, f"input_boolean.{item_id}"
                    for _ in range(int(HELPER_WAIT / 0.5)):  # the entity is added a moment after the item is stored
                        ok, entry, _ = await command(ws, {"type": "config/entity_registry/get", "entity_id": out["entity_id"]})
                        if ok and isinstance(entry, dict):
                            out["registered"] = True
                            break
                        await asyncio.sleep(0.5)
                    if out["registered"]:
                        ok, _, _ = await command(ws, {"type": "config/entity_registry/update", "entity_id": out["entity_id"], "categories": {"helpers": cat_id}})
                        out["categorised"] = bool(ok)
        except Exception as e0:  # noqa: BLE001 - the WebSocket client raises groups of exceptions; classify the first real one
            e: BaseException = e0
            while isinstance(e, BaseExceptionGroup) and e.exceptions:
                e = e.exceptions[0]
            if isinstance(e, HAError):
                raise e from None
            if isinstance(e, ImportError):
                raise HAError("wsproto is missing: install the plugin's packages from its page. Nothing was created.") from None
            if isinstance(e, TimeoutError):
                raise HAError("Home Assistant did not answer in time" + (", so the category or the helper may or may not have been created. Check Home Assistant's helpers before trying again."
                                                                     if create_sent else ". Nothing was created.")) from None
            self._log.warning("home assistant helper command failed: %s", type(e).__name__)
            if _cause_is_tls(e):
                raise HAError("Home Assistant's certificate was not accepted for the WebSocket. Check ca_certificate.") from None
            raise HAError("The WebSocket to Home Assistant failed" + (", so the category or the helper may or may not have been created. Check Home Assistant's helpers before trying again."
                                                                  if create_sent else ". Nothing was created.")) from None
        self._states = None  # the entity list and the registry are now stale
        self._registry = None
        return out

    # ------------------------------------------------------------- registry
    async def registry(self) -> Registry:
        now = time.monotonic()
        if self._registry and now - self._registry[0] < REGISTRY_TTL:
            return self._registry[1]
        async with self._reg_lock:
            if self._registry and time.monotonic() - self._registry[0] < REGISTRY_TTL:
                return self._registry[1]
            if self._registry_failed and time.monotonic() - self._registry_failed[0] < REGISTRY_RETRY:
                raise self._registry_failed[1]  # do not retry a failing WebSocket on every call
            try:
                reg = await self._fetch_registry()
            except RegistryUnavailable as e:
                self._registry_failed = (time.monotonic(), e)
                raise
            self._registry = (time.monotonic(), reg)
            self._registry_failed = None
            return reg

    async def _fetch_registry(self) -> Registry:
        self._blocked()
        s: Settings = self._get()
        ws_url = ("wss://" if s.base_url.startswith("https://") else "ws://") + s.base_url.split("://", 1)[1] + "/api/websocket"
        commands = {1: {"type": "config/area_registry/list"}, 2: {"type": "config/device_registry/list"}, 3: {"type": "config/entity_registry/list"},
                    4: {"type": "config/category_registry/list", "scope": "automation"}, 5: {"type": "config/category_registry/list", "scope": "helpers"}}
        optional = {4, 5}  # categories are only needed for automations and helpers; an older Home Assistant may not have the command
        unlisted: set[int] = set()
        results: dict[int, Any] = {}
        try:
            async with asyncio.timeout(s.timeout * 3):
                async with self._http.websocket(ws_url, max_message_size_bytes=s.max_response_bytes, keepalive_ping_interval_seconds=None) as ws:
                    hello = await ws.receive_json(timeout=s.timeout)
                    if not isinstance(hello, dict) or hello.get("type") != "auth_required":
                        raise RegistryUnavailable("Home Assistant's WebSocket did not start as expected. Check the base_url setting.")
                    await ws.send_json({"type": "auth", "access_token": s.token})
                    reply = await ws.receive_json(timeout=s.timeout)
                    if not isinstance(reply, dict) or reply.get("type") == "auth_invalid":
                        raise self._refused()
                    if reply.get("type") != "auth_ok":
                        raise RegistryUnavailable("Home Assistant's WebSocket did not accept the connection.")
                    for i, payload in commands.items():
                        await ws.send_json({"id": i, **payload})
                    while len(results) < len(commands):
                        msg = await ws.receive_json(timeout=s.timeout)
                        if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("id") in commands:
                            if not msg.get("success") or not isinstance(msg.get("result"), list):
                                if msg["id"] in optional:
                                    results[msg["id"]] = []
                                    unlisted.add(msg["id"])
                                    continue
                                raise RegistryUnavailable("Home Assistant would not list its areas and devices. The token's user may not be allowed.")
                            results[msg["id"]] = msg["result"]
        except Exception as e0:  # noqa: BLE001 - the WebSocket client raises groups of exceptions; classify the first real one
            e: BaseException = e0
            while isinstance(e, BaseExceptionGroup) and e.exceptions:
                e = e.exceptions[0]
            if isinstance(e, HAError):
                raise e from None
            if isinstance(e, ImportError):
                raise RegistryUnavailable("wsproto is missing: install the plugin's packages from its page to get area and device data. Everything else works.") from None
            if isinstance(e, TimeoutError):
                raise RegistryUnavailable("Home Assistant did not answer the area and device request in time.") from None
            self._log.warning("home assistant registry read failed: %s", type(e).__name__)
            if _cause_is_tls(e):
                raise RegistryUnavailable("Home Assistant's certificate was not accepted for the WebSocket. Check ca_certificate.") from None
            raise RegistryUnavailable("Cannot read area and device data from Home Assistant (the WebSocket failed).") from None
        areas = {a["area_id"]: str(a.get("name") or "") for a in results[1] if isinstance(a, dict) and isinstance(a.get("area_id"), str)}
        device_area = {d["id"]: d.get("area_id") for d in results[2] if isinstance(d, dict) and isinstance(d.get("id"), str)}
        entity_area: dict[str, str] = {}
        entity_category: dict[str, str] = {}
        for e in results[3]:
            if isinstance(e, dict) and isinstance(e.get("entity_id"), str):
                area = e.get("area_id") or device_area.get(e.get("device_id"))
                if isinstance(area, str):
                    entity_area[e["entity_id"]] = area
                cats = e.get("categories")
                if isinstance(cats, dict) and isinstance(cats.get("automation"), str):
                    entity_category[e["entity_id"]] = cats["automation"]
        categories = {c["category_id"]: str(c.get("name") or "") for c in results[4] if isinstance(c, dict) and isinstance(c.get("category_id"), str)}
        helper_categories = {c["category_id"]: str(c.get("name") or "") for c in results[5] if isinstance(c, dict) and isinstance(c.get("category_id"), str)}
        return Registry(areas, entity_area, categories, entity_category, helper_categories, 4 not in unlisted, 5 not in unlisted)
