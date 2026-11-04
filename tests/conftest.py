"""Test harness: a fake CouchDB, a LiveSync document writer, and a way to run a plugin under the real mcpopus manager."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import sys
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
sys.path.insert(0, str(Path(__file__).parent))


def plugin_module(sub: str, plugin: str = "obsidian"):
    """Import plugins/<plugin>/<sub>.py as part of its package, outside the host (for unit tests)."""
    pkg = f"_t_{plugin}"
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(pkg, PLUGINS / plugin / "__init__.py", submodule_search_locations=[str(PLUGINS / plugin)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module(f"{pkg}.{sub}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def serve_app(app, port: int | None = None):
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.fixture
async def couch():
    from fake_couch import FakeCouch

    fake = FakeCouch(db="vault", user="alex", password="couch-pass-123")
    async with serve_app(fake.app) as url:
        fake.url = url
        yield fake


@contextlib.asynccontextmanager
async def running_plugin(tmp_path: Path, name: str, values: dict, secrets: dict | None = None, policy=None, plugins_dir: Path = PLUGINS):
    """Load a plugin with the real mcpopus manager and yield (MCP client, LoadedPlugin, audit path)."""
    from mcpopus.audit import AuditLog
    from mcpopus.crypto import SecretBox, generate_key
    from mcpopus.manager import PluginManager
    from mcpopus.pluginconfig import ConfigStore

    box = SecretBox.from_text(generate_key())
    configs = ConfigStore(tmp_path / "config", box)
    audit = AuditLog(tmp_path / "audit.jsonl")
    configs.save(name, values=values, new_secrets=secrets or {}, policy=policy)
    try:  # hosts from 0.2.0 record usage statistics; older ones have no such module
        from mcpopus.stats import Stats
        from mcpopus.store import Store

        stats = Stats(Store(tmp_path / "stats.db"))
    except ImportError:
        stats = None
    mgr = PluginManager(plugins_dir, tmp_path / "data", configs, audit, **({"stats": stats} if stats else {}))
    mgr.stats = stats
    mgr.discover()
    await mgr.load(name)
    lp = mgr.get(name)
    try:
        async with serve_app(lp.app) as url:
            async with httpx2.AsyncClient(timeout=30) as http:
                async with Client(streamable_http_client(url + "/", http_client=http)) as client:
                    yield client, lp, mgr
    finally:
        await mgr.stop()
