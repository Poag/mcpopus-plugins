"""allow_own_helpers: Claude may use the helpers it created without list entries. Design section 9e.

The rule is one fixed thing: input_boolean.turn_on, turn_off and toggle on input_boolean.<claude_prefix>* entities. Everything else keeps needing the lists.
"""

import pytest
from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import st
from test_homeassistant import HOME, SECRETS, call, ha, values  # noqa: F401  (ha is a fixture)

ALL = Policy(read_only=False, enable_tools=["call_service", "save_automation", "create_helper"])
AUTO = {"automation_id": "claude_chime", "alias": "Doorbell chime", "triggers": [{"type": "state", "entity_id": "event.doorbell_doorbell"}],
        "actions": [{"service": "input_boolean.turn_on", "entity_id": "input_boolean.claude_chime"}, {"delay": "00:00:03"},
                    {"service": "input_boolean.turn_off", "entity_id": "input_boolean.claude_chime"}]}


def home(ha):
    for e, name in (("input_boolean.claude_chime", "Chime"), ("input_boolean.car_now", "Car now"), ("input_boolean.bot_flag", "Bot flag")):
        ha.states[e] = st(e, "off", friendly_name=name)
    ha.states["event.doorbell_doorbell"] = st("event.doorbell_doorbell", "unknown", friendly_name="Doorbell")


def cfg(ha, **kw):
    return values(ha, **kw)  # no allowed_services and no write_entity_allowlist unless a test adds them


async def svc(c, service, entity, **kw):
    d, s = service.split(".")
    return await call(c, "call_service", {"domain": d, "service": s, "entity_id": entity, **kw})


async def test_off_by_default_nothing_is_allowed_without_the_lists(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha), SECRETS, policy=ALL) as (c, _, _):
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime")
        assert err and "No such service." in t
        err, t = await call(c, "save_automation", AUTO)
        assert err and "allowed_services" in t
        assert "allow_own_helpers" not in (await call(c, "status"))[1] and "No services are allowed" in (await call(c, "list_services"))[1]
    assert ha.service_calls == [] and ha.saved == []


async def test_when_on_claude_switches_its_own_helpers_with_no_list_entries(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, allow_own_helpers=True), SECRETS, policy=ALL) as (c, _, _):
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime")
        assert not err and "Called input_boolean.turn_on on input_boolean.claude_chime." in t and "before: off" in t and "after: on" in t, t
        assert not (await svc(c, "input_boolean.toggle", "input_boolean.claude_chime"))[0] and ha.states["input_boolean.claude_chime"]["state"] == "off"
        assert not (await svc(c, "input_boolean.turn_off", "input_boolean.claude_chime"))[0]
        assert not (await call(c, "save_automation", AUTO))[0]  # and in the actions of an automation it saves
        status = (await call(c, "status"))[1]
        assert "allow_own_helpers is on" in status and "input_boolean.claude_* helpers (1 exist now)" in status
        assert "Also, because allow_own_helpers is on: input_boolean.turn_on, input_boolean.turn_off, input_boolean.toggle on entities named input_boolean.claude_*" in (await call(c, "list_services"))[1]
    assert [(d, s) for d, s, _ in ha.service_calls] == [("input_boolean", "turn_on"), ("input_boolean", "toggle"), ("input_boolean", "turn_off")]
    assert ha.saved[0]["initial_state"] is False  # still saved disabled


async def test_it_reaches_nothing_else(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, allow_own_helpers=True), SECRETS, policy=ALL) as (c, _, _):
        for service, entity, expect in (("input_boolean.turn_on", "input_boolean.car_now", "may not change that entity"),   # not the prefix
                                        ("input_boolean.turn_on", "input_boolean.bot_flag", "may not change that entity"),  # another prefix
                                        ("input_boolean.reload", "input_boolean.claude_chime", "No such service."),         # a service beyond the three
                                        ("input_boolean.set_value", "input_boolean.claude_chime", "No such service."),
                                        ("light.turn_on", "light.kitchen", "No such service."),                              # another domain
                                        ("switch.turn_on", "switch.garage_fan", "No such service.")):
            err, t = await svc(c, service, entity)
            assert err and expect in t, (service, entity, t)
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime", data={"area_id": "kitchen"})
        assert err and "may not name targets" in t  # the data checks still apply
        for bad in (dict(AUTO, actions=[{"service": "input_boolean.turn_on", "entity_id": "input_boolean.car_now"}]),
                    dict(AUTO, actions=[{"service": "input_boolean.reload", "entity_id": "input_boolean.claude_chime"}]),
                    dict(AUTO, actions=[{"service": "input_boolean.turn_on", "entity_id": ["input_boolean.claude_chime", "input_boolean.car_now"]}])):
            assert (await call(c, "save_automation", bad))[0]
    assert ha.service_calls == [] and ha.saved == []


async def test_the_prefix_setting_is_the_namespace(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, allow_own_helpers=True, claude_prefix="bot_"), SECRETS, policy=ALL) as (c, _, _):
        assert not (await svc(c, "input_boolean.turn_on", "input_boolean.bot_flag"))[0]
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime")
        assert err and "may not change that entity" in t


async def test_the_read_policy_still_hides_helpers_and_a_denied_domain_switches_the_rule_off(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path / "a", HOME, cfg(ha, allow_own_helpers=True, entity_denylist=["input_boolean.claude_*"]), SECRETS, policy=ALL) as (c, _, _):
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime")
        assert err and "No such entity." in t  # hidden looks missing
        status = (await call(c, "status"))[1]
        assert "(0 exist now)" in status  # a hidden helper is not counted either
    async with running_plugin(tmp_path / "b", HOME, cfg(ha, allow_own_helpers=True, domain_denylist=["input_boolean", "lock"]), SECRETS, policy=ALL) as (c, _, _):
        err, t = await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime")
        assert err and "No such service." in t
        status = (await call(c, "status"))[1]
        assert "no effect" in status and "No services are allowed" in (await call(c, "list_services"))[1]
    async with running_plugin(tmp_path / "c", HOME, cfg(ha, allow_own_helpers=True, entity_allowlist=["light.*"]), SECRETS, policy=ALL) as (c, _, _):
        assert (await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime"))[0]  # outside the allow list: invisible
    assert ha.service_calls == []


async def test_the_owners_lists_still_work_alongside_and_do_not_leak_the_implied_services(ha, tmp_path):
    home(ha)
    lists = dict(allowed_services=["input_boolean.turn_on"], write_entity_allowlist=["input_boolean.car_*"])
    async with running_plugin(tmp_path, HOME, cfg(ha, allow_own_helpers=True, **lists), SECRETS, policy=ALL) as (c, _, _):
        assert not (await svc(c, "input_boolean.turn_on", "input_boolean.car_now"))[0]       # the owner's own entry
        err, t = await svc(c, "input_boolean.turn_off", "input_boolean.car_now")             # an implied service does not reach a listed entity
        assert err and "may not change that entity" in t
        assert not (await svc(c, "input_boolean.turn_off", "input_boolean.claude_chime"))[0]  # but the own helper has all three
    assert [(s, e) for _, s, e in [(d, s, b["entity_id"]) for d, s, b in ha.service_calls]] == [("turn_on", "input_boolean.car_now"), ("turn_off", "input_boolean.claude_chime")]


async def test_the_write_rate_limit_still_applies(ha, tmp_path):
    home(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, allow_own_helpers=True, max_writes_per_minute=1), SECRETS, policy=ALL) as (c, _, _):
        assert not (await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime"))[0]
        err, t = await svc(c, "input_boolean.turn_off", "input_boolean.claude_chime")
        assert err and "max_writes_per_minute" in t


async def test_create_helper_says_whether_the_new_helper_is_usable_straight_away(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, cfg(ha, claude_category="Claude", allow_own_helpers=True), SECRETS, policy=ALL) as (c, _, _):
        err, t = await call(c, "create_helper", {"helper_id": "doorbell_chime"})
        assert not err and "allow_own_helpers is on, so Claude may switch it on, off and toggle it" in t
        assert not (await svc(c, "input_boolean.turn_on", "input_boolean.claude_doorbell_chime"))[0]  # and it really can, with no list entry
    async with running_plugin(tmp_path / "b", HOME, cfg(ha, claude_category="Claude"), SECRETS, policy=ALL) as (c, _, _):
        err, t = await call(c, "create_helper", {"helper_id": "other_flag"})
        assert not err and "allow_own_helpers" not in t and "does not match write_entity_allowlist" in t


async def test_wildcards_the_entity_list_takes_globs_and_the_service_list_does_not(ha, tmp_path):
    """The answer to 'a wildcard in the lists': entities yes (already), services no (exact pairs)."""
    from mcpopus.manager import PluginLoadError

    home(ha)
    lists = dict(allowed_services=["input_boolean.turn_on", "input_boolean.turn_off"], write_entity_allowlist=["input_boolean.claude_*"])
    async with running_plugin(tmp_path / "a", HOME, cfg(ha, **lists), SECRETS, policy=ALL) as (c, _, _):
        assert not (await svc(c, "input_boolean.turn_on", "input_boolean.claude_chime"))[0]       # one glob covers every helper Claude will create
        assert (await svc(c, "input_boolean.toggle", "input_boolean.claude_chime"))[0]              # but services are still listed one by one
        assert (await svc(c, "input_boolean.turn_on", "input_boolean.car_now"))[0]
    for wild in ("input_boolean.*", "*.turn_on", "input_boolean.turn_*"):
        with pytest.raises(PluginLoadError) as e:
            async with running_plugin(tmp_path / ("w" + str(abs(hash(wild)))), HOME, cfg(ha, allowed_services=[wild]), SECRETS):
                pass
        assert "allowed_services" in str(e.value)


def test_is_own_helper_unit():
    from conftest import plugin_module

    p = plugin_module("policy", HOME)
    assert p.is_own_helper("input_boolean.claude_chime", "claude_") and p.is_own_helper("INPUT_BOOLEAN.Claude_x", "claude_")
    for no in ("input_boolean.chime", "input_boolean.bot_chime", "light.claude_chime", "switch.claude_chime", "sensor.claude_chime", "input_boolean.xclaude_chime", "claude_chime", "input_boolean.", ""):
        assert not p.is_own_helper(no, "claude_"), no
    assert p.OWN_HELPER_SERVICES == ("input_boolean.turn_on", "input_boolean.turn_off", "input_boolean.toggle")  # fixed: changing it changes what the setting allows
