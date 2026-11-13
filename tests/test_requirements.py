"""Plugin manifests and imports against the host's dependency rules (host 0.3.0: docs/PLUGINS.md, "Dependencies")."""

import ast
import importlib.metadata
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import PLUGINS

# What the host image carries for plugins to import (host docs/PLUGINS.md, "Dependencies", host 0.3.0), plus the host itself.
HOST_MODULES = {"mcpopus", "mcp", "starlette", "uvicorn", "pydantic", "cryptography", "argon2", "jinja2", "httpx2", "segno", "pip"}
MIN_HOST_WITH_REQUIREMENTS = (0, 3, 0)
PLUGIN_DIRS = sorted(p for p in PLUGINS.iterdir() if (p / "plugin.toml").exists())


def manifest(plugin: Path) -> dict:
    return tomllib.loads((plugin / "plugin.toml").read_text())


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def module_names(requirements: list[str]) -> set[str]:
    """Top-level import names provided by the distributions a plugin lists (read from what is installed in the test environment)."""
    from mcpopus.packages import normalise

    wanted = {normalise(r.split("==")[0]) for r in requirements}
    return {mod for mod, dists in importlib.metadata.packages_distributions().items() if wanted & {normalise(d) for d in dists}}


def third_party_imports(source: str) -> set[str]:
    """Top-level names a source file imports (import statements, plus importlib.util.find_spec / import_module with a literal name)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            fn = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if fn in ("find_spec", "import_module"):
                found.add(node.args[0].value.split(".")[0])
    return {n for n in found if n not in sys.stdlib_module_names}


@pytest.mark.parametrize("plugin", PLUGIN_DIRS, ids=lambda p: p.name)
def test_requirements_pass_the_hosts_parser(plugin):
    from mcpopus.packages import parse_requirements

    m = manifest(plugin)
    reqs = m.get("requirements", [])
    assert parse_requirements(reqs) == reqs  # exact pins, normalised spacing, all hashed or none, at most 20, no duplicates
    if reqs:
        assert version_tuple(m["min_host_version"]) >= MIN_HOST_WITH_REQUIREMENTS, "an older host would fail on an import instead of refusing the plugin"


@pytest.mark.parametrize("plugin", PLUGIN_DIRS, ids=lambda p: p.name)
def test_no_import_outside_the_hosts_list_or_the_plugins_requirements(plugin):
    allowed = HOST_MODULES | module_names(manifest(plugin).get("requirements", []))
    for f in plugin.rglob("*.py"):
        stray = third_party_imports(f.read_text()) - allowed
        assert not stray, f"{f.relative_to(PLUGINS)} imports {sorted(stray)}, which the host does not provide and the plugin does not list in requirements"


def test_the_import_check_catches_an_unlisted_package():
    """Mutation check: the scan must see a direct import, a lazy one and a find_spec name, and the allowed set must not hide them."""
    src = "import yaml\nfrom wsproto import WSConnection\nimport importlib.util\nimportlib.util.find_spec('h11')\nimport json, os\nfrom . import x\n"
    assert third_party_imports(src) == {"yaml", "wsproto", "h11"}
    assert {"yaml", "wsproto", "h11"} - HOST_MODULES == {"yaml", "wsproto", "h11"}


def test_the_packages_each_plugin_needs_are_listed():
    """The plugins that were found to need packages when this was written, and what they list. Update with the plugin, not to make this pass."""
    wanted = {"homeassistant": {"wsproto", "h11"}, "swarmui": {"wsproto", "h11"}, "obsidian": {"pyyaml"}}
    from mcpopus.packages import normalise

    for plugin in PLUGIN_DIRS:
        listed = {normalise(r.split("==")[0]) for r in manifest(plugin).get("requirements", [])}
        assert listed == wanted[plugin.name], plugin.name


def test_every_pin_matches_the_test_environment():
    """requirements-dev.txt must install the same versions the manifests ask for, so the tests exercise what the host will install."""
    from mcpopus.packages import normalise

    for plugin in PLUGIN_DIRS:
        for r in manifest(plugin).get("requirements", []):
            name, version = r.split()[0].split("==")
            assert importlib.metadata.version(normalise(name)) == version, f"{plugin.name}: {name} {version} is not what the test environment has"


async def test_a_plugin_whose_packages_are_not_installed_does_not_load_and_says_so(tmp_path):
    """The host (not this repository) refuses the load; this shows the harness's marking is what lets the other tests load the plugins."""
    from mcpopus.audit import AuditLog
    from mcpopus.crypto import SecretBox, generate_key
    from mcpopus.manager import PluginLoadError, PluginManager
    from mcpopus.packages import Packages
    from mcpopus.pluginconfig import ConfigStore

    configs = ConfigStore(tmp_path / "config", SecretBox.from_text(generate_key()))
    configs.save("obsidian", values={"couchdb_url": "http://127.0.0.1:1", "database": "vault"}, new_secrets={}, policy=None)
    mgr = PluginManager(PLUGINS, tmp_path / "data", configs, AuditLog(tmp_path / "audit.jsonl"), packages=Packages(tmp_path / "data", enabled=False))
    mgr.discover()
    with pytest.raises(PluginLoadError, match="packages that are not installed"):
        await mgr.load("obsidian")
    mgr_old = PluginManager(PLUGINS, tmp_path / "data2", configs, AuditLog(tmp_path / "audit2.jsonl"))  # a host with no package support
    mgr_old.discover()
    with pytest.raises(PluginLoadError, match="needs packages"):
        await mgr_old.load("obsidian")
