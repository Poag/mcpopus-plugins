"""A fake CouchDB that serves only what the plugin needs, records every request, and rejects writes."""

from __future__ import annotations

import base64

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class FakeCouch:
    def __init__(self, db: str, user: str, password: str) -> None:
        self.db, self.user, self.password = db, user, password
        self.docs: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self.changes: list[tuple[int, str, bool]] = []  # (seq, id, deleted)
        self.url = ""
        self.read_only = False  # emulate a CouchDB user without write access
        self.before_put = None  # test hook: called with the document about to be stored
        self.app = Starlette(routes=[
            Route("/{db}", self.info, methods=["GET"]),
            Route("/{db}/_all_docs", self.all_docs, methods=["GET", "POST"]),
            Route("/{db}/_bulk_docs", self.bulk_docs, methods=["POST"]),
            Route("/{db}/_changes", self.changes_feed, methods=["POST", "GET"]),
            Route("/{db}/{doc_id:path}", self.doc, methods=["GET", "PUT", "DELETE", "POST"]),
        ])

    # ---- test-side helpers
    def load(self, docs: list[dict]) -> None:
        for d in docs:
            self.put(d)

    def put(self, doc: dict) -> None:
        self.docs[doc["_id"]] = doc
        self.changes.append((len(self.changes) + 1, doc["_id"], False))

    def remove(self, doc_id: str) -> None:
        self.docs.pop(doc_id, None)
        self.changes.append((len(self.changes) + 1, doc_id, True))

    # ---- handlers
    def _check(self, request: Request) -> JSONResponse | None:
        self.log.append((request.method, request.url.path))
        auth = request.headers.get("authorization", "")
        want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if auth != want:
            return JSONResponse({"error": "unauthorized"}, 401)
        if request.path_params.get("db") != self.db:
            return JSONResponse({"error": "not_found", "reason": "Database does not exist."}, 404)
        return None

    async def info(self, request: Request):
        return self._check(request) or JSONResponse({"db_name": self.db, "doc_count": len(self.docs)})

    async def doc(self, request: Request):
        bad = self._check(request)
        if bad:
            return bad
        doc_id = request.path_params["doc_id"]
        if request.method == "PUT":
            if self.read_only:
                return JSONResponse({"error": "forbidden", "reason": "read only"}, 403)
            body = await request.json()
            if body.get("_id") != doc_id:
                return JSONResponse({"error": "bad_request"}, 400)
            res = self._store(body)
            return JSONResponse(res[0], res[1])
        if request.method != "GET":
            return JSONResponse({"error": "forbidden"}, 403)
        d = self.docs.get(doc_id)
        return JSONResponse(d) if d else JSONResponse({"error": "not_found", "reason": "missing"}, 404)

    def _store(self, body: dict):
        cur = self.docs.get(body["_id"])
        if cur is not None and body.get("_rev") != cur["_rev"]:
            return {"error": "conflict", "reason": "Document update conflict."}, 409
        if cur is None and body.get("_rev"):
            return {"error": "conflict", "reason": "Document update conflict."}, 409
        if self.before_put:
            self.before_put(body)
        gen = int(cur["_rev"].split("-")[0]) + 1 if cur else 1
        body = {**body, "_rev": f"{gen}-{abs(hash(str(sorted(body.items(), key=str)))):x}"}
        self.docs[body["_id"]] = body
        self.changes.append((len(self.changes) + 1, body["_id"], False))
        self.writes.append(body["_id"])
        return {"ok": True, "id": body["_id"], "rev": body["_rev"]}, 201

    async def bulk_docs(self, request: Request):
        bad = self._check(request)
        if bad:
            return bad
        if self.read_only:
            return JSONResponse({"error": "forbidden"}, 403)
        out = []
        for d in (await request.json())["docs"]:
            body, status = self._store(d)
            out.append({"id": d["_id"], "ok": True, "rev": body["rev"]} if status == 201 else {"id": d["_id"], "error": "conflict", "reason": body["reason"]})
        return JSONResponse(out, 201)

    async def all_docs(self, request: Request):
        bad = self._check(request)
        if bad:
            return bad
        if request.method == "POST":
            keys = (await request.json())["keys"]
            with_docs = request.query_params.get("include_docs") == "true"
            rows = [({"id": k, "key": k, "doc": self.docs[k]} if with_docs else {"id": k, "key": k, "value": {"rev": self.docs[k]["_rev"]}})
                    if k in self.docs else {"key": k, "error": "not_found"} for k in keys]
        else:
            rows = [{"id": k, "key": k, "doc": d} for k, d in list(sorted(self.docs.items()))[: int(request.query_params.get("limit", "1000"))]]
        return JSONResponse({"total_rows": len(self.docs), "rows": rows})

    async def changes_feed(self, request: Request):
        bad = self._check(request)
        if bad:
            return bad
        q = request.query_params
        if request.method != "POST" or q.get("filter") != "_selector":
            return JSONResponse({"error": "bad_request"}, 400)
        wanted = (await request.json())["selector"]["type"]["$in"]
        since, limit = int(q.get("since", "0")), int(q.get("limit", "100"))
        results, last = [], since
        latest: dict[str, tuple[int, bool]] = {}
        for seq, _id, deleted in self.changes:
            latest[_id] = (seq, deleted)
        for _id, (seq, deleted) in sorted(latest.items(), key=lambda kv: kv[1][0]):
            if seq <= since:
                continue
            if len(results) >= limit:
                break
            doc = self.docs.get(_id)
            if deleted:
                results.append({"seq": seq, "id": _id, "changes": [], "deleted": True})
            elif doc and doc.get("type") in wanted:
                results.append({"seq": seq, "id": _id, "changes": [], "doc": doc})
            last = seq
        else:
            last = max(last, self.changes[-1][0] if self.changes else since)
        return JSONResponse({"results": results, "last_seq": last, "pending": 0})
