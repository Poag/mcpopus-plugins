"""call_service: off by default, narrow when on, and every refusal happens before anything reaches Home Assistant."""

import logging

import pytest
from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import TOKEN, FakeHA
from test_homeassistant import DENIED, HOME, SECRETS, body, call, ha, values  # noqa: F401  (ha is a fixture)

ENABLED = Policy(read_only=False, enable_tools=["call_service"])
SERVICES = ["light.turn_on", "light.turn_off", "switch.turn_on", "switch.turn_off", "cover.open_cover", "cover.close_cover", "climate.set_temperature"]


def wcfg(ha, **kw):
    return values(ha, allowed_services=SERVICES, write_entity_allowlist=["light.*"], **kw)


async def svc(c, domain="light", service="turn_on", entity_id="light.kitchen", **kw):
    return await call(c, "call_service", {"domain": domain, "service": service, "entity_id": entity_id, **kw})


async def test_off_until_enabled_by_name_and_refused_at_the_server(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, wcfg(ha), SECRETS) as (c, _, _):  # default policy: read-only
        assert "call_service" not in [t.name for t in (await c.list_tools()).tools]
        err, t = await svc(c)
        assert err and "Unknown tool" in t
    async with running_plugin(tmp_path / "b", HOME, wcfg(ha), SECRETS, policy=Policy(read_only=True, enable_tools=["call_service"])) as (c, _, _):
        assert (await svc(c))[0]  # enabled by name but the read-only switch is still on
    async with running_plugin(tmp_path / "c", HOME, wcfg(ha), SECRETS, policy=Policy(read_only=False)) as (c, _, _):
        assert (await svc(c))[0]  # read-only is off but the tool is not enabled by name
    assert ha.service_calls == [] and ha.writes == []  # the fake saw nothing


async def test_nothing_is_allowed_by_default_even_when_the_tool_is_on(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, values(ha), SECRETS, policy=ENABLED) as (c, _, _):
        err, t = await svc(c)
        assert err and "No such service." in t
    async with running_plugin(tmp_path / "b", HOME, values(ha, allowed_services=["light.turn_on"]), SECRETS, policy=ENABLED) as (c, _, _):
        err, t = await svc(c)
        assert err and "may not change that entity" in t  # a service is named but no entity may be changed
    assert ha.service_calls == []


async def test_allowed_call_succeeds_with_before_and_after(ha, tmp_path):
    ha.extra_changed = ["lock.front_door", "light.hall"]  # an automation also moved a hidden lock and a visible light
    async with running_plugin(tmp_path, HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        err, t = await svc(c, data={"brightness": 90})
        text = body(t)
        assert not err and "Called light.turn_on on light.kitchen." in text
        assert "before: on (brightness=180)" in text and "after: on (brightness=90)" in text
        assert "light.hall" in text and "lock.front_door" not in text  # changed hidden entities are not reported
        err, t = await svc(c, service="turn_off")
        assert "after: off" in body(t)
    assert ha.service_calls[0] == ("light", "turn_on", {"brightness": 90, "entity_id": "light.kitchen"})
    assert [s[:2] for s in ha.service_calls] == [("light", "turn_on"), ("light", "turn_off")]
    assert not any("template" in w for w in ha.writes) and all(w.startswith("POST /api/services/") for w in ha.writes)


async def test_services_not_allowed_look_like_services_that_do_not_exist(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        missing = await svc(c, domain="light", service="no_such_service_anywhere")  # not allowed, and not in Home Assistant either
        assert missing[0] and "No such service." in missing[1]
        for d, s, e in (("lock", "unlock", "lock.front_door"), ("light", "toggle", "light.kitchen"), ("homeassistant", "turn_off", "light.kitchen"),
                        ("notify", "mobile_app_alex", "notify.mobile_app_alex")):
            got = await svc(c, domain=d, service=s, entity_id=e)
            assert got[0] and got[1] == missing[1], (d, s)
        for bad in ("a b", "x/y", "../x", "", "light.turn_on"):
            assert (await svc(c, domain="light", service=bad))[0]
    # an allowed pair that Home Assistant does not offer answers the same way
    ha.services = [s for s in ha.services if s["domain"] != "switch"]
    async with running_plugin(tmp_path / "b", HOME, values(ha, allowed_services=["switch.turn_on"], write_entity_allowlist=["switch.*"]), SECRETS, policy=ENABLED) as (c, _, _):
        got = await svc(c, domain="switch", service="turn_on", entity_id="switch.garage_fan")
        assert got[0] and got == missing
    assert ha.service_calls == []


async def test_hidden_missing_and_unwritable_entities(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, wcfg(ha, entity_denylist=["light.hall"]), SECRETS, policy=ENABLED) as (c, _, _):
        missing = await svc(c, entity_id="light.not_a_real_light")
        assert missing[0] and "No such entity." in missing[1]
        for e in ("light.hall",):  # hidden by the read policy
            assert await svc(c, entity_id=e) == missing
        for d, s, e in (("lock", "unlock", "lock.front_door"),):
            assert (await svc(c, domain=d, service=s, entity_id=e))[0]
        # visible, exists, but not on the write list: the answer may say so, because the entity is visible anyway
        err, t = await svc(c, domain="switch", service="turn_on", entity_id="switch.garage_fan")
        assert err and "may not change that entity" in t
        # not on the write list AND missing: must not differ from a hidden entity, so it says "No such entity."
        got = await svc(c, domain="switch", service="turn_on", entity_id="switch.not_real")
        assert got[0] and "No such entity." in got[1]
        err, t = await svc(c, domain="light", service="turn_on", entity_id="switch.garage_fan")
        assert err and "same domain" in t
    assert ha.service_calls == []


async def test_targets_cannot_be_widened(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        for e in ("all", "light.kitchen,light.hall", "light.*", "area.kitchen", "light.kitchen light.hall", ""):
            err, t = await svc(c, entity_id=e)
            assert err
        r = await c.call_tool("call_service", {"domain": "light", "service": "turn_on", "entity_id": ["light.kitchen", "light.hall"]})
        assert r.is_error
        for key in ("entity_id", "area_id", "device_id", "label_id", "floor_id", "target"):
            err, t = await svc(c, data={key: "kitchen"})
            assert err and "may not name targets" in t, key
        for data, expect in (({"x": "light.hall"}, "other entities"), ({"x": ["a", {"y": "lock.front_door"}]}, "other entities"), ("brightness=5", "valid dict"),
                             ({"Bad Key": 1}, "field names"), ({f"k{i}": 1 for i in range(21)}, "at most 20"), ({"x": "y" * 2100}, "too large")):
            err, t = await svc(c, data=data)
            assert err and expect in t, data
        err, t = await svc(c, data={"effect": "rainbow", "brightness": 10, "same": "light.kitchen"})
        assert not err  # naming the target itself in the data is harmless
    assert [s[2]["entity_id"] for s in ha.service_calls] == ["light.kitchen"]


async def test_rate_limit(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, wcfg(ha, max_writes_per_minute=3), SECRETS, policy=ENABLED) as (c, _, _):
        results = [await svc(c) for _ in range(5)]
        assert [r[0] for r in results] == [False, False, False, True, True]
        assert "max_writes_per_minute" in results[3][1]
        # refusals before the call do not use up the allowance
        assert (await svc(c, domain="lock", service="unlock", entity_id="lock.front_door"))[0]
    assert len(ha.service_calls) == 3


async def test_errors_from_home_assistant_are_clear_and_safe(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        ha.call_status = 400
        err, t = await svc(c)
        assert err and "Home Assistant rejected the call. Check that the service accepts that entity" in t
        ha.call_status = 500
        err, t = await svc(c)
        assert err and "reported an error while carrying out the call" in t and "Check the entity's state" in t
        ha.call_status = None
    ha.call_delay = 5
    async with running_plugin(tmp_path / "b", HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        err, t = await svc(c)
        assert err and "the action may or may not have happened" in t
    ha.call_delay = 0
    async with running_plugin(tmp_path / "c", HOME, wcfg(ha), {"token": "wrong-tok-xyz"}, policy=ENABLED) as (c, _, _):
        err, t = await svc(c)
        assert err and "refused the access token" in t and "wrong-tok-xyz" not in t
    assert ha.service_calls == [] or all(s[0] == "light" for s in ha.service_calls)


async def test_token_never_appears_in_write_output_logs_or_audit(ha, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with running_plugin(tmp_path, HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        out = [(await svc(c, data={"brightness": 50}))[1], (await svc(c, service="turn_off"))[1], (await svc(c, entity_id="light.nope"))[1]]
    assert TOKEN not in "\n".join(out) and TOKEN not in caplog.text and TOKEN not in (tmp_path / "audit.jsonl").read_text()
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "brightness" not in audit and "light.kitchen" not in audit  # argument names only, never values


async def test_list_services_and_status_describe_the_write_setup(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, wcfg(ha), SECRETS, policy=ENABLED) as (c, _, _):
        _, t = await call(c, "list_services")
        assert "Entities call_service may act on: light.*" in t and "light.turn_on | Turn on a light." in t
        assert "cover.open_cover | not offered by Home Assistant" in t
        _, t = await call(c, "status")
        assert "7 service(s) allowed; 1 entity pattern(s)" in t
    async with running_plugin(tmp_path / "b", HOME, values(ha, allowed_services=["light.turn_on"]), SECRETS) as (c, _, _):
        assert "nothing can be changed" in (await call(c, "status"))[1]


async def test_write_config_is_validated(ha, tmp_path):
    from mcpopus.manager import PluginLoadError

    for extra, field in (({"allowed_services": ["light.turn_on, light.turn_off"]}, "allowed_services"), ({"write_entity_allowlist": ["lock.*"]}, "write_entity_allowlist"), ({"write_entity_allowlist": ["Bad Pattern"]}, "write_entity_allowlist"),
                         ({"max_writes_per_minute": 0}, "max_writes_per_minute"), ({"max_writes_per_minute": 1000}, "max_writes_per_minute")):
        with pytest.raises(PluginLoadError) as e:
            async with running_plugin(tmp_path / field / str(len(str(extra))), HOME, values(ha, **extra), SECRETS):
                pass
        assert field in str(e.value) and "Bad Pattern" not in str(e.value)
