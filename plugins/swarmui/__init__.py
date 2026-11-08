"""swarmui: generate images with a SwarmUI instance, within limits the plugin enforces. Design: docs/DESIGN-swarmui.md."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import secrets
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx2
from mcpopus.plugin import BaseModel, Field, Plugin, PluginHost, SecretStr, ToolError, tool
from pydantic import field_validator

from . import text as tx
from .client import Settings, Swarm, SwarmError, check_base_url, ssl_context
from .policy import JOB_ID_RE, MODEL_NAME_RE, PARAM_ID_RE, ModelPolicy, check_prompt, forbidden_param, norm_glob, safe_image_path

NEEDED_PERMISSIONS = ("fundamental_generate_tab_access", "fundamental_model_access", "basic_image_generation", "view_image_history")
HARMLESS_PERMISSIONS = frozenset(NEEDED_PERMISSIONS) | {"fundamental", "read_user_settings", "use_tokenizer"}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_PERM = re.compile(r"^[a-z0-9_]{1,60}$")
MAX_JOBS_KEPT = 50


class Config(BaseModel):
    base_url: str = Field(description="SwarmUI address, for example http://swarm.local:7801 (no path, no credentials)")
    auth_token: SecretStr | None = Field(None, description="A SwarmUI auth token (User tab, Swarm Auth Tokens), sent as the swarm_token cookie. Needed when the instance requires accounts, which it should. Make it for a dedicated user with a limited role")
    verify_tls: bool = Field(True, description="Check the server certificate for https. Turn off only on a trusted network, and prefer ca_certificate")
    ca_certificate: str = Field("", max_length=20_000, description="Extra certificate authority in PEM form, for a private certificate. Leave empty for a public certificate")
    public_url: str = Field("", description="Where a person opens SwarmUI in a browser, for links in results (for example https://swarm.example.com). Optional")
    model_allowlist: list[str] = Field([], description="Only models and LoRAs matching one of these patterns can be used or seen (one per line, for example SDXL/*). Empty means all except the denied ones")
    model_denylist: list[str] = Field([], description="Models and LoRAs matching these patterns are never used or seen (one per line). Always wins")
    allowed_parameters: list[str] = Field([], description="Extra SwarmUI parameter ids that generate_image's extra argument may set (one per line, for example sampler). Empty means none. Image inputs, outputs and video can never be listed")
    allow_prompt_syntax: bool = Field(False, description="Allow <...> tags in prompts. They can load models, read files on the server and change settings, so leave off")
    default_model: str = Field("", description="Model used when the caller names none")
    max_images_per_call: int = Field(2, ge=1, le=8, description="Most images in one generation")
    max_pixels: int = Field(1_048_576, ge=65_536, le=16_777_216, description="Most pixels in one image (width times height)")
    max_steps: int = Field(40, ge=1, le=150, description="Most sampling steps")
    max_prompt_chars: int = Field(1500, ge=50, le=4000, description="Longest prompt or negative prompt")
    max_concurrent_jobs: int = Field(1, ge=1, le=4, description="Generations this connector may have running at once")
    max_queue: int = Field(3, ge=0, le=50, description="Refuse to start when SwarmUI already has this many generations waiting")
    max_per_minute: int = Field(4, ge=1, le=60, description="Most generations started per minute")
    max_per_hour: int = Field(30, ge=1, le=500, description="Most generations started per hour")
    wait_seconds: int = Field(45, ge=5, le=120, description="How long generate_image waits for the images before returning a job id to check later")
    job_timeout_seconds: int = Field(600, ge=30, le=3600, description="Longest a generation is waited for before it is interrupted and marked failed")
    progress_updates: bool = Field(True, description="Use SwarmUI's WebSocket for live progress when the host supports it (needs the wsproto library); otherwise a plain request is used")
    max_results: int = Field(20, ge=1, le=100, description="Most models or images listed per call")
    instructions: str = Field("", max_length=4000, description="Text shown to the AI client with this connector (which models to prefer, how you like prompts). Do not put secrets here")
    timeout_seconds: int = Field(20, ge=3, le=120, description="Seconds to wait for SwarmUI on ordinary requests")
    max_response_bytes: int = Field(4_000_000, ge=100_000, le=100_000_000, description="Largest reply accepted from SwarmUI")

    @field_validator("base_url", "public_url", "default_model", "ca_certificate", mode="before")
    @classmethod
    def _trim(cls, v):  # pasted values often carry a stray space or newline
        return v.strip() if isinstance(v, str) else v

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return check_base_url(v)

    @field_validator("public_url")
    @classmethod
    def _public(cls, v: str) -> str:
        return check_base_url(v) if v else v

    @field_validator("auth_token")
    @classmethod
    def _token(cls, v):
        if v is None:
            return v
        raw = v.get_secret_value().strip()
        if not raw:
            return None
        if re.search(r"[\s;,\"'\\]", raw) or _CTRL.search(raw):
            raise ValueError("must be the token only: no spaces, quotes, semicolons or line breaks (copy the whole token)")
        return v

    @field_validator("ca_certificate")
    @classmethod
    def _ca(cls, v: str) -> str:
        if v:
            try:
                ssl.create_default_context().load_verify_locations(cadata=v)
            except (ssl.SSLError, ValueError):
                raise ValueError("must be one or more PEM certificates (-----BEGIN CERTIFICATE-----)") from None
        return v

    @field_validator("default_model")
    @classmethod
    def _model(cls, v: str) -> str:
        if v and not MODEL_NAME_RE.fullmatch(v):
            raise ValueError("is not a usable model name")
        return v

    @field_validator("model_allowlist", "model_denylist", mode="before")
    @classmethod
    def _globs(cls, v):
        return [norm_glob(x) for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else v

    @field_validator("allowed_parameters", mode="before")
    @classmethod
    def _params(cls, v):
        if not isinstance(v, list):
            return v
        out = [x.strip().lower() for x in v if isinstance(x, str) and x.strip()]
        for p in out:
            if not PARAM_ID_RE.fullmatch(p):
                raise ValueError("parameter ids are lower-case letters, digits and underscores, for example sampler")
            if forbidden_param(p):
                raise ValueError("lists a parameter that can never be set (a reserved name, an image input, an output location, video or audio); remove it")
        return out


@dataclass
class Job:
    id: str
    model: str
    width: int
    height: int
    count: int
    created: float = field(default_factory=time.time)
    state: str = "queued"  # queued, running, done, failed, cancelled
    percent: float | None = None
    images: list[str] = field(default_factory=list)
    hidden: int = 0
    error: str = ""
    cancel_requested: bool = False
    finished: float = 0.0
    task: asyncio.Task | None = None


class SwarmUIPlugin(Plugin):
    def __init__(self, ctx, http: httpx2.AsyncClient) -> None:
        super().__init__(ctx)
        cfg = ctx.config
        self.policy = ModelPolicy(tuple(cfg.model_allowlist), tuple(cfg.model_denylist))
        self.sw = Swarm(http, self._settings, ctx.logger)
        self.jobs: dict[str, Job] = {}  # per-process and disposable: a reload forgets them (outputs stay in SwarmUI's history)
        self._started: deque[float] = deque()
        self.instructions = (cfg.instructions or "").strip() or None

    def _settings(self) -> Settings:
        c = self.ctx.config
        return Settings(c.base_url, c.auth_token.get_secret_value().strip() if c.auth_token else "", float(c.timeout_seconds), c.max_response_bytes, float(c.job_timeout_seconds))  # read at the moment of use

    # ------------------------------------------------------------- plumbing
    @staticmethod
    async def _guard(coro):
        try:
            return await coro
        except SwarmError as e:
            raise ToolError(str(e)) from None

    @staticmethod
    def _refuse(message: str):
        raise ToolError(message)

    def _link(self, path: str) -> str:
        base = self.ctx.config.public_url
        if not base:
            return ""
        return f"{base}/" + quote(path if path.startswith("View/") else f"View/{path}", safe="/")

    def _limit(self, limit: int) -> int:
        cap = self.ctx.config.max_results
        return cap if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 else min(limit, cap)

    async def _visible_models(self, subtype: str) -> list[dict]:
        files = await self._guard(self.sw.models(subtype))
        return [f for f in files if self.policy.visible(f["name"])]

    @staticmethod
    def _offset(offset: int) -> int:
        return max(0, offset if isinstance(offset, int) and not isinstance(offset, bool) else 0)

    # ---------------------------------------------------------------- reads
    @tool(tier="read")
    async def status(self) -> str:
        """Check the connection and token, and summarise SwarmUI: version, the account in use, queue, backends, whether its permissions are broader than needed, and the limits this connector applies."""
        st = await self._guard(self.sw.status())  # first, so a session that had expired is replaced before its permissions are read
        sess = await self._guard(self.sw.session())
        cfg = self.ctx.config
        version = sess.version if re.fullmatch(r"[0-9A-Za-z.+_-]{1,40}", sess.version) else "unknown"
        user = tx.clean(sess.user_id, 60) or "unknown"
        perms = sorted({p for p in sess.permissions if _PERM.fullmatch(p)})
        missing = [p for p in NEEDED_PERMISSIONS if p not in perms]
        extra = [p for p in perms if p not in HARMLESS_PERMISSIONS]
        status = st.get("status") if isinstance(st.get("status"), dict) else {}
        backend = st.get("backend_status") if isinstance(st.get("backend_status"), dict) else {}
        num = lambda k: int(status.get(k, 0)) if isinstance(status.get(k), (int, float)) else 0  # noqa: E731
        ws_ok = cfg.progress_updates and importlib.util.find_spec("wsproto") is not None
        running = sum(1 for j in self.jobs.values() if j.state in ("queued", "running"))
        lines = [
            f"Connected to SwarmUI {version}; signed in as {user}.",
            f"Queue: {num('waiting_gens')} waiting, {num('loading_models')} loading models, {num('live_gens')} running, {num('waiting_backends')} waiting for a backend.",
            f"Backends: {tx.clean(backend.get('status', 'unknown'), 30)}" + (f" ({tx.clean(backend['message'], 120)})" if backend.get("message") else "") + ".",
            f"Permissions: {len(perms)} granted. " + ("All the ones this connector needs are present." if not missing else f"Missing, so some tools will fail: {', '.join(missing)}."),
        ]
        if extra:
            lines.append(f"This account can do more than the connector needs ({len(extra)} extra permissions: {', '.join(extra[:30])}{' ...' if len(extra) > 30 else ''}). "
                         "Safer: make a dedicated SwarmUI user whose role holds only " + ", ".join(NEEDED_PERMISSIONS) + ", and use its token.")
        if not cfg.auth_token:
            lines.append("No auth_token is set: requests run as whatever account SwarmUI gives anonymous callers. If SwarmUI requires accounts, set one.")
        lines += [
            f"Limits: {cfg.max_images_per_call} image(s) per call, {cfg.max_pixels} pixels, {cfg.max_steps} steps, {cfg.max_per_minute} a minute, {cfg.max_per_hour} an hour, "
            f"{cfg.max_concurrent_jobs} at once, queue at most {cfg.max_queue}. Prompt syntax with <...> is {'allowed' if cfg.allow_prompt_syntax else 'refused'}.",
            f"Model policy: {len(cfg.model_allowlist)} allow pattern(s); {len(cfg.model_denylist)} deny pattern(s).",
            f"Live progress over WebSocket: {'on' if ws_ok else 'off (the host lacks the wsproto library, or progress_updates is off)'}. Generations running from this connector: {running}.",
            "Generation is only available if the owner ticked generate_image under Policy. Images are not shown to Claude: results give file paths" + (" and links." if cfg.public_url else "; set public_url to get links."),
        ]
        return "\n".join(lines)

    async def _list(self, subtype: str, query: str, limit: int, offset: int) -> str:
        query = str(query or "").strip().casefold()
        if len(query) > 100:
            raise ToolError("Search values are limited to 100 characters.")
        limit, offset = self._limit(limit), self._offset(offset)
        files = await self._visible_models(subtype)
        words = query.split()
        hits = sorted((f for f in files if all(w in f["name"].casefold() for w in words)), key=lambda f: f["name"].casefold())
        page = hits[offset: offset + limit]
        head = f"{len(hits)} match (showing {offset + 1 if page else 0} to {offset + len(page)})."
        if offset + len(page) < len(hits):
            head += f" Use offset {offset + len(page)} for the next page."
        rows = []
        for f in page:
            size = f"{f['standard_width']}x{f['standard_height']}" if isinstance(f.get("standard_width"), int) and isinstance(f.get("standard_height"), int) else "-"
            rows.append(f"- {tx.clean(f['name'], 200)} | {tx.clean(f.get('class') or f.get('architecture') or '-', 40)} | {size}" + (" | loaded" if f.get("loaded") is True else ""))
        return "\n".join([head, *rows])

    @tool(tier="read", untrusted=True)
    async def list_models(self, query: str = "", limit: int = 0, offset: int = 0) -> str:
        """List the image models (checkpoints) this connector may use: name, class, standard size. query: words that must all appear in the name."""
        return await self._list("Stable-Diffusion", query, limit, offset)

    @tool(tier="read", untrusted=True)
    async def list_loras(self, query: str = "", limit: int = 0, offset: int = 0) -> str:
        """List the LoRAs this connector may use. query: words that must all appear in the name."""
        return await self._list("LoRA", query, limit, offset)

    @tool(tier="read", untrusted=True)
    async def describe_model(self, name: str, kind: str = "model") -> str:
        """Describe one model (kind "model") or LoRA (kind "lora") by its exact name from the list: title, author, trigger phrase, tags, standard size, usage hint."""
        kind = str(kind or "model").strip().lower()
        if kind not in ("model", "lora"):
            raise ToolError('kind must be "model" or "lora".')
        subtype = "LoRA" if kind == "lora" else "Stable-Diffusion"
        name = str(name or "").strip()
        names = {f["name"] for f in await self._visible_models(subtype)}
        if name not in names:
            return "No such model."  # hidden and missing look the same
        m = await self._guard(self.sw.describe(name, subtype))
        if m is None:
            return "No such model."
        tags = m.get("tags") if isinstance(m.get("tags"), list) else []
        lines = [f"name: {tx.clean(name, 200)}", f"title: {tx.clean(m.get('title') or '-', 100)}", f"author: {tx.clean(m.get('author') or '-', 80)}",
                 f"class: {tx.clean(m.get('class') or m.get('architecture') or '-', 60)}", f"loaded: {'yes' if m.get('loaded') is True else 'no'}",
                 f"standard size: {m.get('standard_width', '-')}x{m.get('standard_height', '-')}", f"license: {tx.clean(m.get('license') or '-', 80)}",
                 f"trigger phrase: {tx.clean(m.get('trigger_phrase') or '-', 200)}", f"usage hint: {tx.clean(m.get('usage_hint') or '-', 300)}",
                 f"tags: {', '.join(tx.clean(t, 30) for t in tags[:10]) or '-'}", f"description: {tx.clean(m.get('description') or '-', 500)}"]
        return "\n".join(lines)

    @tool(tier="read", untrusted=True)
    async def list_parameters(self) -> str:
        """List the generation settings generate_image accepts, with their allowed ranges, as SwarmUI reports them."""
        cfg = self.ctx.config
        params = await self._guard(self.sw.params())
        base = ("prompt", "negativeprompt", "model", "images", "width", "height", "steps", "cfgscale", "seed")
        lines = []
        for pid in (*base, *cfg.allowed_parameters):
            p = params.get(pid)
            if p is None:
                lines.append(f"- {pid} | not offered by this SwarmUI" if pid in cfg.allowed_parameters else f"- {pid} | (built in)")
                continue
            vals = p.get("values") if isinstance(p.get("values"), list) else None
            lines.append(f"- {pid} | {tx.clean(p.get('type', '-'), 20)} | default {tx.clean(p.get('default', '-'), 40)} | min {tx.clean(p.get('min', '-'), 12)} | max {tx.clean(p.get('max', '-'), 12)}"
                         + (f" | values: {', '.join(tx.clean(v, 30) for v in vals[:12])}" if vals else "") + f" | {tx.clean(p.get('description', ''), 120)}")
        lines.append(f"Limits here: images 1 to {cfg.max_images_per_call}; pixels at most {cfg.max_pixels}; width and height multiples of 8; steps at most {cfg.max_steps}; seed -1 for random.")
        return "\n".join(lines)

    @tool(tier="read", untrusted=True)
    async def list_images(self, folder: str = "", contains: str = "", limit: int = 0) -> str:
        """List recent images in the account's history, newest first: path, model, size and the start of the prompt. folder: a sub-folder; contains: text the file or prompt must contain."""
        folder = str(folder or "").strip().strip("/")
        contains = str(contains or "").strip()
        if len(contains) > 100 or (folder and (".." in folder or not safe_image_path(folder))):
            raise ToolError("folder must be a plain relative folder name, and contains is limited to 100 characters.")
        limit = self._limit(limit)
        data = await self._guard(self.sw.images(folder, 2, contains))
        rows = []
        for f in data.get("files", []) if isinstance(data.get("files"), list) else []:
            src = safe_image_path(f.get("src")) if isinstance(f, dict) else None
            if not src:
                continue
            params = {}
            meta = f.get("metadata")
            if isinstance(meta, str) and meta[:1] == "{" and len(meta) < 200_000:
                try:
                    parsed = json.loads(meta).get("sui_image_params")
                    params = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    params = {}
            model = params.get("model")
            if isinstance(model, str) and model and not self.policy.visible(model):
                continue  # an image made with a model this connector may not see is left out
            size = f"{params['width']}x{params['height']}" if params.get("width") and params.get("height") else "-"
            link = self._link(src)
            rows.append(f"- {src} | {tx.clean(model or '-', 80)} | {tx.clean(size, 20)} | prompt: {tx.clean(params.get('prompt', ''), 100)}" + (f" | {link}" if link else ""))
            if len(rows) >= limit:
                break
        return "\n".join([f"{len(rows)} image(s), newest first.", *rows])

    # ----------------------------------------------------------- generation
    def _job_text(self, job: Job) -> str:
        head = f"job {job.id}: {job.state}"
        if job.state in ("queued", "running"):
            pct = f", about {int(job.percent * 100)}% done" if job.percent is not None else ""
            return f"{head}{pct} ({job.count} image(s), {job.model}, {job.width}x{job.height}). Check again with get_generation (job_id {job.id})."
        lines = [f"{head} ({job.count} image(s), {job.model}, {job.width}x{job.height})"]
        if job.error:
            lines.append(f"error: {job.error}")
        for path in job.images:
            link = self._link(path)
            lines.append(f"- {path}" + (f" | {link}" if link else ""))
        if job.hidden:
            lines.append(f"({job.hidden} image(s) came back in a form that is not shown here; find them in SwarmUI's history.)")
        if job.images:
            lines.append("Claude cannot see these images: open them in SwarmUI" + ("" if self.ctx.config.public_url else " (set public_url for links)") + ".")
        return "\n".join(lines)

    def _prune(self) -> None:
        if len(self.jobs) <= MAX_JOBS_KEPT:
            return
        for jid, j in sorted(self.jobs.items(), key=lambda kv: kv[1].created):
            if j.state not in ("queued", "running") and len(self.jobs) > MAX_JOBS_KEPT:
                del self.jobs[jid]

    @staticmethod
    def _num(value, name: str, integer: bool = True):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"{name} must be a number.")
        if integer and float(value) != int(value):
            raise ToolError(f"{name} must be a whole number.")
        return int(value) if integer else float(value)

    def _in_range(self, pid: str, value, params: dict, what: str) -> None:
        p = params.get(pid) or {}
        for bound, op in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
            lim = p.get(bound)
            if isinstance(lim, (int, float)) and not isinstance(lim, bool) and op(float(value), float(lim)):
                raise ToolError(f"{what} is outside the range SwarmUI allows ({bound} {lim}).")

    async def _build(self, prompt, negative_prompt, model, width, height, steps, cfg_scale, seed, images, loras, extra) -> tuple[dict, str]:
        cfg = self.ctx.config
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("Give a prompt describing the image.")
        for text, what in ((prompt, "The prompt"), (negative_prompt, "The negative prompt")):
            problem = check_prompt(text, what, cfg.max_prompt_chars, cfg.allow_prompt_syntax)
            if problem:
                raise ToolError(problem)
        images = self._num(images, "images")
        if not 1 <= images <= cfg.max_images_per_call:
            raise ToolError(f"images must be 1 to {cfg.max_images_per_call} (the max_images_per_call setting).")
        width, height = self._num(width, "width"), self._num(height, "height")
        if width % 8 or height % 8 or not (64 <= width <= 4096 and 64 <= height <= 4096):
            raise ToolError("width and height must be multiples of 8 between 64 and 4096.")
        if width * height > cfg.max_pixels:
            raise ToolError(f"That size is more than this connector allows ({cfg.max_pixels} pixels, the max_pixels setting).")
        steps = self._num(steps, "steps")
        if not 1 <= steps <= cfg.max_steps:
            raise ToolError(f"steps must be 1 to {cfg.max_steps} (the max_steps setting).")
        cfg_scale = self._num(cfg_scale, "cfg_scale", integer=False)
        if not 0 <= cfg_scale <= 30:
            raise ToolError("cfg_scale must be between 0 and 30.")
        seed = self._num(seed, "seed")
        if not -1 <= seed <= 2**53:
            raise ToolError("seed must be -1 (random) or a whole number up to 9007199254740992.")
        model = str(model or cfg.default_model or "").strip()
        if not model:
            raise ToolError("Name a model (see list_models), or ask the owner to set default_model.")
        if model not in {f["name"] for f in await self._visible_models("Stable-Diffusion")}:
            return self._refuse("No such model.")  # unknown, hidden and denied are one answer
        tags = []
        if loras:
            if not isinstance(loras, list) or len(loras) > 4:
                raise ToolError("loras is a list of at most 4 items like {\"name\": \"...\", \"weight\": 0.8}.")
            known = {f["name"] for f in await self._visible_models("LoRA")}
            for item in loras:
                if not isinstance(item, dict) or set(item) - {"name", "weight"}:
                    raise ToolError("Each lora is {\"name\": \"...\", \"weight\": 0.8}.")
                name = str(item.get("name") or "").strip()
                weight = self._num(item.get("weight", 1.0), "lora weight", integer=False)
                if not 0 < weight <= 2:
                    raise ToolError("A lora weight must be above 0 and at most 2.")
                if name not in known or not MODEL_NAME_RE.fullmatch(name):
                    return self._refuse("No such LoRA.")
                tags.append(f"<lora:{name}:{weight:g}>")
        params = await self._guard(self.sw.params())
        for pid, value in (("width", width), ("height", height), ("steps", steps), ("cfgscale", cfg_scale)):
            self._in_range(pid, value, params, pid)
        payload: dict = {}
        if extra:
            if not isinstance(extra, dict) or len(extra) > 10:
                raise ToolError("extra is an object of at most 10 settings.")
            for key, value in extra.items():
                if not isinstance(key, str) or key not in cfg.allowed_parameters:
                    raise ToolError("extra may only set settings the owner listed in allowed_parameters.")
                spec = params.get(key)
                if spec is None:
                    raise ToolError("extra names a setting this SwarmUI does not offer.")
                if isinstance(value, bool) or isinstance(value, (int, float)):
                    if not isinstance(value, bool):
                        self._in_range(key, value, params, key)
                elif isinstance(value, str):
                    problem = check_prompt(value, f"The {key} value", 500, cfg.allow_prompt_syntax)
                    if problem:
                        raise ToolError(problem)
                else:
                    raise ToolError("extra values must be text, numbers or true/false.")
                allowed = spec.get("values") if isinstance(spec.get("values"), list) else None
                if allowed and str(value) not in {str(a) for a in allowed}:
                    raise ToolError(f"{key} must be one of the values SwarmUI lists (see list_parameters).")
                payload[key] = value
        full_prompt = (prompt.strip() + (" " + " ".join(tags) if tags else ""))
        payload.update({"images": images, "prompt": full_prompt, "negativeprompt": (negative_prompt or "").strip(), "model": model,
                        "width": width, "height": height, "steps": steps, "cfgscale": cfg_scale, "seed": seed})
        return payload, model

    def _take_slot(self) -> None:
        cfg = self.ctx.config
        now = time.monotonic()
        while self._started and now - self._started[0] > 3600:
            self._started.popleft()
        if sum(1 for t in self._started if now - t <= 60) >= cfg.max_per_minute:
            raise ToolError(f"Too many generations in the last minute ({cfg.max_per_minute}, the max_per_minute setting). Wait and try again.")
        if len(self._started) >= cfg.max_per_hour:
            raise ToolError(f"Too many generations in the last hour ({cfg.max_per_hour}, the max_per_hour setting).")
        self._started.append(now)

    @tool(tier="write", untrusted=True)
    async def generate_image(self, prompt: str, model: str = "", negative_prompt: str = "", width: int = 1024, height: int = 1024, steps: int = 20,
                             cfg_scale: float = 7.0, seed: int = -1, images: int = 1, loras: list[dict] | None = None, extra: dict | None = None) -> str:
        """Generate one or more images from a text prompt with SwarmUI, using a model from list_models. Waits briefly, then returns the saved image paths, or a job id to check with get_generation. loras: up to 4 items like {"name": "...", "weight": 0.8} from list_loras. Prompts may not contain '<'. Claude cannot see the images."""
        cfg = self.ctx.config
        payload, model = await self._build(prompt, negative_prompt, model, width, height, steps, cfg_scale, seed, images, loras, extra)
        if sum(1 for j in self.jobs.values() if j.state in ("queued", "running")) >= cfg.max_concurrent_jobs:
            raise ToolError(f"{cfg.max_concurrent_jobs} generation(s) already running from this connector (the max_concurrent_jobs setting). Wait for it, or cancel it.")
        st = await self._guard(self.sw.status())
        waiting = st.get("status", {}).get("waiting_gens", 0) if isinstance(st.get("status"), dict) else 0
        if isinstance(waiting, (int, float)) and waiting >= cfg.max_queue:
            raise ToolError(f"SwarmUI already has {int(waiting)} generation(s) waiting (the max_queue setting). Try again later.")
        self._take_slot()
        self._prune()
        job = Job(secrets.token_hex(4), model, payload["width"], payload["height"], payload["images"])
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, payload))
        self.ctx.logger.info("generation started: job %s, %d image(s), %dx%d", job.id, job.count, job.width, job.height)
        stats = getattr(self.ctx, "stats", None)
        if stats is not None:
            stats.count("generations", 1)
        await asyncio.wait({job.task}, timeout=cfg.wait_seconds)
        return self._job_text(job)

    async def _run(self, job: Job, payload: dict) -> None:
        cfg = self.ctx.config
        job.state = "running"
        try:
            async with asyncio.timeout(cfg.job_timeout_seconds):
                if cfg.progress_updates and importlib.util.find_spec("wsproto") is not None:
                    got = await self.sw.generate_ws(payload, lambda p: setattr(job, "percent", p))
                else:
                    got = await self.sw.generate_http(payload)
            shown = [p for p in (safe_image_path(i) for i in got) if p]
            job.images, job.hidden = shown, len(got) - len(shown)
            if shown:
                job.state = "cancelled" if job.cancel_requested else "done"
            else:
                job.state = "cancelled" if job.cancel_requested else "failed"
                job.error = "" if job.cancel_requested else "SwarmUI finished without returning an image."
        except asyncio.CancelledError:
            job.state, job.error = "cancelled", ""
            raise
        except TimeoutError:
            job.state, job.error = "failed", f"The generation did not finish within {cfg.job_timeout_seconds} seconds (the job_timeout_seconds setting) and was interrupted."
            try:
                await asyncio.wait_for(self.sw.interrupt(), 5)
            except Exception:  # noqa: BLE001 - best effort
                pass
        except SwarmError as e:
            job.state, job.error = ("cancelled", "") if job.cancel_requested else ("failed", str(e))
        except Exception as e:  # noqa: BLE001
            self.ctx.logger.warning("generation failed: %s", type(e).__name__)
            job.state, job.error = "failed", "The generation failed."
        finally:
            job.finished = time.time()

    @tool(tier="read", untrusted=True)
    async def get_generation(self, job_id: str) -> str:
        """Check a generation started by generate_image: queued, running (with progress when available), done (with image paths), failed or cancelled."""
        jid = str(job_id or "").strip().lower()
        job = self.jobs.get(jid) if JOB_ID_RE.fullmatch(jid) else None
        if job is None:
            return "No such job. Jobs are forgotten when the connector reloads; recent images are in list_images."
        return self._job_text(job)

    @tool(tier="write")
    async def cancel_generation(self) -> str:
        """Interrupt the generations this connector has running. It affects only this connector's own SwarmUI session, never other users or other tabs."""
        running = [j for j in self.jobs.values() if j.state in ("queued", "running")]
        if not running:
            return "Nothing is running from this connector."
        for j in running:
            j.cancel_requested = True
        await self._guard(self.sw.interrupt())
        return f"Asked SwarmUI to interrupt {len(running)} running generation(s) from this connector."


async def setup(host: PluginHost) -> None:
    ctx = host.context
    cfg = ctx.config
    http = httpx2.AsyncClient(verify=ssl_context(cfg.verify_tls, cfg.ca_certificate), timeout=httpx2.Timeout(float(cfg.timeout_seconds)),
                              follow_redirects=False, limits=httpx2.Limits(max_connections=6))
    await host.add_plugin(SwarmUIPlugin(ctx, http))  # no network work here: problems surface on use


async def teardown(host: PluginHost) -> None:
    plugin = next(iter(getattr(host, "_registered", [])), None)
    if plugin is None:
        return
    running = [j for j in plugin.jobs.values() if j.task is not None and not j.task.done()]
    for j in running:
        j.cancel_requested = True
        j.task.cancel()
    if running:
        try:
            await asyncio.wait_for(plugin.sw.interrupt(), 3)  # best effort: do not leave an orphan generation running
        except Exception:  # noqa: BLE001
            pass
    await plugin.sw.aclose()
