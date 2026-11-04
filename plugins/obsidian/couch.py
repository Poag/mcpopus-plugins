"""A small async CouchDB client: only the read calls the plugin needs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

MAX_BATCH = 200


class CouchError(Exception):
    """Message is safe to show: a status or a reason, never credentials, URLs with credentials or document content."""


class Conflict(CouchError):
    """The document was changed by someone else since it was read (HTTP 409)."""


class CouchDB:
    def __init__(self, http: httpx2.AsyncClient, url: str, database: str, user: str, password: str) -> None:
        url = url.strip()
        parts = urlsplit(url)
        if "://" not in url or parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query:
            raise CouchError("the CouchDB URL must be http(s)://host[:port][/prefix] without credentials")
        self._http = http
        self._base = f"{url.rstrip('/')}/{quote(database.strip(), safe='')}"
        self._auth = (user, password) if user else None

    async def _req(self, method: str, path: str, doc: bool = False, **kw: Any) -> Any:
        try:
            r = await self._http.request(method, self._base + path, auth=self._auth, headers={"Accept": "application/json"}, **kw)
        except httpx2.TimeoutException:
            raise CouchError("CouchDB did not answer in time") from None
        except httpx2.UnsupportedProtocol:
            raise CouchError("the CouchDB URL is not usable: it must start with http:// or https:// and contain no spaces") from None
        except httpx2.ConnectError:
            raise CouchError("cannot connect to CouchDB: check the host name and port, and that CouchDB is running") from None
        except httpx2.HTTPError as e:
            raise CouchError(f"cannot reach CouchDB ({type(e).__name__})") from None
        if r.status_code == 404:
            raise CouchError("document not found" if doc else "database not found (check the database name)")
        if r.status_code == 409:
            raise Conflict("the note was changed by someone else while it was being written; read it again and retry")
        if r.status_code in (401, 403):
            raise CouchError("CouchDB refused the credentials or permission (a read-only CouchDB user cannot write)")
        if r.status_code >= 400:
            raise CouchError(f"CouchDB returned status {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise CouchError("CouchDB returned something that is not JSON") from None

    async def info(self) -> dict[str, Any]:
        return await self._req("GET", "")

    async def get_local(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return await self._req("GET", "/" + quote(doc_id, safe="/"), doc=True)
        except CouchError as e:
            if "document not found" in str(e):
                return None
            raise

    async def bulk_get(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch documents by id; missing ones are absent from the result."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), MAX_BATCH):
            res = await self._req("POST", "/_all_docs", params={"include_docs": "true"}, json={"keys": ids[i : i + MAX_BATCH]})
            for row in res.get("rows", []):
                if row.get("doc"):
                    out[row["id"]] = row["doc"]
        return out

    async def changes(self, since: str | int, selector: dict[str, Any], limit: int = 500) -> dict[str, Any]:
        return await self._req(
            "POST", "/_changes", params={"filter": "_selector", "include_docs": "true", "since": str(since), "limit": str(limit)},
            json={"selector": selector},
        )

    async def sample(self, limit: int = 200) -> list[dict[str, Any]]:
        """Documents in id order, for diagnostics only."""
        res = await self._req("GET", "/_all_docs", params={"include_docs": "true", "limit": str(limit)})
        return [r["doc"] for r in res.get("rows", []) if r.get("doc")]

    # ---- writes (used only by the write tools, which are off until the owner enables them)
    async def get_doc(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return await self._req("GET", "/" + quote(doc_id, safe=""), doc=True)
        except CouchError as e:
            if "document not found" in str(e):
                return None
            raise

    async def put_doc(self, doc: dict[str, Any]) -> str:
        """Create or replace one document. The document's own _rev must be the current one, or Conflict is raised."""
        res = await self._req("PUT", "/" + quote(doc["_id"], safe=""), json=doc)
        return res.get("rev", "")

    async def existing_ids(self, ids: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(ids), MAX_BATCH):
            res = await self._req("POST", "/_all_docs", json={"keys": ids[i : i + MAX_BATCH]})
            found.update(r["id"] for r in res.get("rows", []) if "error" not in r and r.get("value") and not r["value"].get("deleted"))
        return found

    async def bulk_write(self, docs: list[dict[str, Any]]) -> None:
        """Write new documents (chunks). A conflict means the id already exists, which for content-addressed chunks is fine."""
        for i in range(0, len(docs), MAX_BATCH):
            res = await self._req("POST", "/_bulk_docs", json={"docs": docs[i : i + MAX_BATCH]})
            for r in res if isinstance(res, list) else []:
                if r.get("error") and r["error"] != "conflict":
                    raise CouchError(f"CouchDB could not store a chunk ({r['error']})")
