"""Automation definitions: read, create or replace, delete. Fenced by id prefix, a strict subset, the write list, and a separate admin token."""

import copy
import logging

import pytest
from mcpopus.pluginconfig import Policy

from conftest import plugin_module, running_plugin
from fake_ha import TOKEN
from test_homeassistant import HOME, SECRETS, body, call, ha, values  # noqa: F401  (ha is a fixture)

ALL = Policy(read_only=False, enable_tools=["save_automation", "delete_automation", "call_service"])
GOOD = {"automation_id": "claude_porch", "alias": "Porch when door opens", "triggers": [{"type": "state", "entity_id": "binary_sensor.door_contact", "to": "on"}],
        "actions": [{"service": "light.turn_on", "entity_id": "light.kitchen", "data": {"brightness": 120}}, {"delay": "00:00:10"}, {"service": "light.turn_off", "entity_id": "light.kitchen"}]}


def dcfg(ha, **kw):
    return values(ha, allowed_services=["light.turn_on", "light.turn_off", "switch.turn_on"], write_entity_allowlist=["light.*"], **kw)


async def save(c, **kw):
    return await call(c, "save_automation", {**GOOD, **kw})


async def test_tiers_and_default_off(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS) as (c, lp, _):
        names = [t.name for t in (await c.list_tools()).tools]
        assert "get_automation_definition" in names and "save_automation" not in names and "delete_automation" not in names
        assert (await save(c))[0] and (await call(c, "delete_automation", {"automation_id": "claude_porch"}))[0]
    async with running_plugin(tmp_path / "b", HOME, dcfg(ha), SECRETS, policy=Policy(read_only=False, enable_tools=["save_automation"])) as (c, _, _):
        assert (await call(c, "delete_automation", {"automation_id": "claude_porch"}))[0]  # each tool is enabled by its own name
    assert ha.saved == [] and not any(m != "GET" for m, _ in ha.config_calls)


async def test_a_token_whose_user_is_not_an_administrator_only_loses_the_definition_tools(ha, tmp_path):
    ha.token_is_admin = False
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        err, t = await save(c)
        assert err and "automation config call" in t and "administrator" in t and "Everything else still works" in t and TOKEN not in t
        first = ha.auth_failures
        for tool, args in (("save_automation", GOOD), ("delete_automation", {"automation_id": "claude_porch"}), ("get_automation_definition", {"entity_id": "automation.porch_evening"})):
            err, t = await call(c, tool, args)
            assert err and "paused" in t, tool
        assert ha.auth_failures == first == 1  # one refusal, then no more attempts while paused (Home Assistant can ban repeated failures)
        # everything else is unaffected, including the ordinary calls and the other write tools
        assert "access token works" in (await call(c, "status"))[1]
        assert "light.kitchen" in (await call(c, "search_entities", {"query": "kitchen"}))[1]
        assert not (await call(c, "call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}))[0]
    assert ha.saved == [] and all(m == "GET" or cid == "claude_porch" for m, cid in ha.config_calls)


async def test_the_one_token_is_sent_everywhere_and_never_in_output(ha, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        outs = [(await call(c, "status"))[1], (await save(c))[1], (await call(c, "get_automation_definition", {"entity_id": "automation.porch_evening"}))[1],
                (await call(c, "delete_automation", {"automation_id": "claude_porch"}))[1]]
    assert ha.config_calls and {k for _, k in ha.auth_log} == {"user"} and ha.auth_failures == 0
    assert TOKEN not in "\n".join(outs) and TOKEN not in caplog.text and TOKEN not in (tmp_path / "audit.jsonl").read_text()


async def test_save_creates_a_disabled_automation_in_home_assistants_format(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        err, t = await save(c, description="Opens the porch light", conditions=[{"type": "time", "after": "18:00", "weekday": ["mon", "fri"]}])
        text = body(t)
        assert not err and "Created automation claude_porch (Porch when door opens)." in text and "entity: automation.porch_when_door_opens" in text
        assert "1 trigger(s), 1 condition(s), 3 action(s), mode single" in text and "starts DISABLED" in text and "comments in that file are not kept" in text
    saved = ha.saved[0]
    assert saved == {"id": "claude_porch", "alias": "Porch when door opens", "description": "Opens the porch light", "mode": "single", "initial_state": False,
                     "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door_contact", "to": "on"}],
                     "conditions": [{"condition": "time", "after": "18:00", "weekday": ["mon", "fri"]}],
                     "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}, "data": {"brightness": 120}}, {"delay": "00:00:10"},
                                 {"action": "light.turn_off", "target": {"entity_id": "light.kitchen"}}]}
    assert ha.states["automation.porch_when_door_opens"]["state"] == "off"


async def test_update_replaces_keeps_the_owners_initial_state_and_refuses_name_clashes(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        assert not (await save(c))[0]
        ha.automation_configs["claude_porch"]["initial_state"] = True  # the owner changed it in Home Assistant
        err, t = await save(c, alias="Porch renamed", actions=[{"service": "light.turn_on", "entity_id": "light.hall"}])
        assert not err and "Updated automation claude_porch" in t
        assert ha.saved[-1]["initial_state"] is True and ha.saved[-1]["alias"] == "Porch renamed" and len(ha.saved[-1]["actions"]) == 1
        ha.automation_configs["claude_porch"].pop("initial_state")
        assert not (await save(c, alias="Porch renamed again"))[0] and "initial_state" not in ha.saved[-1]
        err, t = await save(c, automation_id="claude_other", alias="porch renamed AGAIN")
        assert err and "already exists" in t
        err, t = await save(c, automation_id="claude_x", alias="Porch Evening")  # a human-written automation's name
        assert err and "already exists" in t


async def test_only_prefixed_ids_can_be_written_or_deleted(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        for bad in ("1001", "porch_evening", "claude", "claude_", "claude_" + "x" * 41, "claude_a b", "claude_../x", "claude_a/b", "claude_a.b", "", "other_porch"):
            err, t = await save(c, automation_id=bad)
            assert err and "must start with claude_" in t, bad
            err, t = await call(c, "delete_automation", {"automation_id": bad})
            assert err and "must start with claude_" in t, bad
        assert "No such automation." in (await call(c, "delete_automation", {"automation_id": "claude_nope"}))[1]
    async with running_plugin(tmp_path / "b", HOME, dcfg(ha, claude_prefix="bot_"), SECRETS, policy=ALL) as (c, _, _):
        assert (await save(c))[0]  # the configured prefix applies
        assert not (await save(c, automation_id="bot_ok"))[0]
    assert [m for m, _ in ha.config_calls if m != "GET"] == ["POST"] and ha.saved[0]["id"] == "bot_ok"
    assert "1001" in ha.automation_configs  # human automations are untouched


async def test_delete_removes_only_the_named_prefixed_automation(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        assert not (await save(c))[0]
        err, t = await call(c, "delete_automation", {"automation_id": "claude_porch"})
        assert not err and "Deleted automation claude_porch" in t
        assert "claude_porch" not in ha.automation_configs and set(ha.automation_configs) == {"1001", "1002", "1003", "1004"}
        assert "No such automation." in (await call(c, "delete_automation", {"automation_id": "claude_porch"}))[1]


async def test_the_subset_refuses_everything_else(ha, tmp_path):
    bad_cases = [
        ({"triggers": [{"type": "event", "event_type": "x"}]}, "Each trigger needs a type"), ({"triggers": [{"type": "webhook"}]}, "Each trigger needs a type"),
        ({"triggers": [{"type": "state", "entity_id": "binary_sensor.door_contact", "to": "{{ 'on' }}"}]}, "template"),
        ({"triggers": [{"type": "state", "entity_id": "lock.front_door"}]}, "domain_denylist"), ({"triggers": [{"type": "state", "entity_id": "sensor.not_real"}]}, "does not exist in Home Assistant"),
        ({"triggers": [{"type": "state", "entity_id": "binary_sensor.door_contact", "platform": "x"}]}, "does not support the field"), ({"triggers": []}, "at least one trigger"),
        ({"triggers": [{"type": "state", "entity_id": "binary_sensor.door_contact"}] * 6}, "at most 5"), ({"triggers": "x"}, "valid list"),
        ({"triggers": [{"type": "numeric_state", "entity_id": "sensor.kitchen_temperature"}]}, "above or below"), ({"triggers": [{"type": "time", "at": "25-00"}]}, "must be a time"),
        ({"triggers": [{"type": "time_pattern", "minutes": "abc"}]}, "number, * or /N"), ({"triggers": [{"type": "sun", "event": "noon"}]}, "sunrise or sunset"),
        ({"conditions": [{"type": "template", "value_template": "{{ true }}"}]}, "Each condition needs a type"), ({"conditions": [{"type": "zone", "entity_id": "person.alex"}]}, "Each condition needs a type"),
        ({"actions": [{"service": "lock.unlock", "entity_id": "lock.front_door"}]}, "allowed_services"), ({"actions": [{"service": "light.toggle", "entity_id": "light.kitchen"}]}, "allowed_services"),
        ({"actions": [{"service": "automation.trigger", "entity_id": "automation.lock_up"}]}, "allowed_services"), ({"actions": [{"service": "script.turn_on", "entity_id": "script.x"}]}, "allowed_services"),
        ({"actions": [{"service": "switch.turn_on", "entity_id": "switch.garage_fan"}]}, "write list"), ({"actions": [{"service": "light.turn_on", "entity_id": "switch.garage_fan"}]}, "same domain"),
        ({"actions": [{"service": "light.turn_on", "entity_id": ["light.kitchen", "light.hall"]}]}, "exactly one entity"), ({"actions": [{"service": "light.turn_on", "entity_id": "all"}]}, "entities like"),
        ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen", "target": {"area_id": "x"}}]}, "does not support the field"),
        ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen", "data": {"area_id": "kitchen"}}]}, "may not name targets"),
        ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen", "data": {"x": "{{ now() }}"}}]}, "template"),
        ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen", "data": {"x": "lock.front_door"}}]}, "other entities"),
        ({"actions": [{"choose": []}]}, "Each action is either"), ({"actions": [{"event": "x"}]}, "Each action is either"), ({"actions": [{"wait_template": "{{ true }}"}]}, "Each action is either"),
        ({"actions": [{"delay": "02:00:00"}, {"service": "light.turn_on", "entity_id": "light.kitchen"}]}, "longer than allowed"), ({"actions": [{"delay": "00:00:05"}]}, "at least one service action"),
        ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen"}], "triggers": [{"type": "state", "entity_id": "light.kitchen", "to": "off"}]}, "trigger itself"),
        ({"alias": ""}, "alias is a short name"), ({"alias": "x" * 101}, "alias is a short name"), ({"alias": "{{ states('x') }}"}, "alias is a short name"), ({"description": "x" * 301}, "description is up to"),
        ({"mode": "parallel"}, "mode is single or restart"), ({"actions": [{"service": "light.turn_on", "entity_id": "light.kitchen"}] * 11}, "at most 10"),
    ]
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        for patch, expect in bad_cases:
            err, t = await save(c, **patch)
            assert err and expect in t, (patch, t[:200])
        assert (await call(c, "save_automation", {"automation_id": "claude_porch", "alias": "A", "triggers": "x"}))[0]
    assert ha.saved == [] and not any(m != "GET" for m, _ in ha.config_calls)  # nothing reached Home Assistant


async def test_every_supported_shape_is_built_correctly(ha, tmp_path):
    spec = {"automation_id": "claude_all", "alias": "Everything", "mode": "restart",
            "triggers": [{"type": "state", "entity_id": ["binary_sensor.door_contact", "sensor.kitchen_temperature"], "from": "off", "to": "on", "for": "00:01:00", "attribute": "brightness"},
                         {"type": "numeric_state", "entity_id": "sensor.kitchen_temperature", "above": 20.5, "below": 30, "for": 90}, {"type": "time", "at": "06:30"},
                         {"type": "time_pattern", "minutes": "/5"}, {"type": "sun", "event": "sunset", "offset": "-00:30:00"}],
            "conditions": [{"type": "state", "entity_id": "switch.garage_fan", "state": ["on", "off"], "for": "00:00:10"}, {"type": "numeric_state", "entity_id": "sensor.kitchen_temperature", "below": 25},
                           {"type": "sun", "after": "sunrise", "before": "sunset", "after_offset": "00:15"}, {"type": "time", "before": "23:00"}],
            "actions": [{"service": "light.turn_on", "entity_id": "light.hall"}, {"delay": 30}]}
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        err, t = await call(c, "save_automation", spec)
        assert not err, t
    s = ha.saved[0]
    assert s["mode"] == "restart" and s["triggers"][0] == {"trigger": "state", "entity_id": ["binary_sensor.door_contact", "sensor.kitchen_temperature"], "from": "off", "to": "on", "for": "00:01:00", "attribute": "brightness"}
    assert s["triggers"][1]["for"] == "00:01:30" and s["triggers"][2] == {"trigger": "time", "at": "06:30"} and s["triggers"][4] == {"trigger": "sun", "event": "sunset", "offset": "-00:30:00"}
    assert s["conditions"][0] == {"condition": "state", "entity_id": "switch.garage_fan", "state": ["on", "off"], "for": "00:00:10"} and s["conditions"][2]["after_offset"] == "00:15"
    assert s["actions"][1] == {"delay": "00:00:30"} and "description" not in s


async def test_reading_definitions_hides_templates_hidden_things_and_missing_ones_alike(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha, entity_denylist=["automation.secret_*"]), SECRETS) as (c, _, _):
        err, t = await call(c, "get_automation_definition", {"entity_id": "automation.porch_evening"})
        text = body(t)
        assert not err and "config id 1001" in text and '"trigger": "state"' in text and "binary_sensor.door_contact" in text and "light.kitchen" in text
        none = await call(c, "get_automation_definition", {"entity_id": "automation.not_real"})
        assert none[0] is False and "No definition available." in none[1]
        for eid in ("automation.garage_night", "automation.lock_up", "automation.secret_routine", "lock.front_door", "light.kitchen"):  # template, hidden entity, denied, wrong domain
            got = await call(c, "get_automation_definition", {"entity_id": eid})
            assert got[0] == none[0] and got[1].replace(eid, "X") == none[1].replace("automation.not_real", "X"), eid
        assert "lock.front_door" not in (await call(c, "get_automation_definition", {"entity_id": "automation.lock_up"}))[1]
    assert not [1 for m, cid in ha.config_calls if cid in ("1002", "1003", "1004") and False]  # (reads of hidden ones may reach Home Assistant; their content never reaches Claude)
    assert not any(m != "GET" for m, _ in ha.config_calls)


async def test_definition_output_is_bounded(ha, tmp_path):
    ha.automation_configs["1001"]["description"] = "d" * 20_000
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS) as (c, _, _):
        _, t = await call(c, "get_automation_definition", {"entity_id": "automation.porch_evening"})
        assert len(t) < 9000 and "[truncated]" in t


async def test_home_assistant_rejection_is_explained_without_its_text(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, dcfg(ha), SECRETS, policy=ALL) as (c, _, _):
        assert not (await save(c))[0]
        for status, expect in ((400, "rejected the automation as invalid"), (500, "reported an error while carrying out the call")):
            ha.config_status = status
            err, t = await save(c, alias="Another name")
            assert err and expect in t and "secret detail" not in t and "/config/" not in t
            err, t = await call(c, "delete_automation", {"automation_id": "claude_porch"})
            assert err and "secret detail" not in t
        ha.config_status = None


async def test_secrets_never_appear_and_rate_limit_is_shared(ha, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with running_plugin(tmp_path, HOME, dcfg(ha, max_writes_per_minute=3), SECRETS, policy=ALL) as (c, _, _):
        out = [(await save(c))[1], (await call(c, "get_automation_definition", {"entity_id": "automation.porch_evening"}))[1], (await call(c, "status"))[1]]
        out += [(await call(c, "call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}))[1], (await save(c))[1], (await save(c))[1]]
        assert "max_writes_per_minute" in out[-1]
    for secret in (TOKEN,):
        assert secret not in "\n".join(out) and secret not in caplog.text and secret not in (tmp_path / "audit.jsonl").read_text()
    assert len(ha.saved) == 2 and len(ha.service_calls) == 1


async def test_config_rules(ha, tmp_path):
    from mcpopus.manager import PluginLoadError

    for extra, secrets, field, leaked in (({"claude_prefix": "Bad Prefix"}, SECRETS, "claude_prefix", "Bad Prefix"), ({"claude_prefix": ""}, SECRETS, "claude_prefix", None),
                                          ({"claude_prefix": "x" * 21}, SECRETS, "claude_prefix", None)):
        with pytest.raises(PluginLoadError) as e:
            async with running_plugin(tmp_path / (field + str(len(str(extra)))), HOME, values(ha, **extra), secrets):
                pass
        assert field in str(e.value) and (not leaked or leaked not in str(e.value))


def test_validator_units():
    a = plugin_module("automations", HOME)
    assert a.has_template("{{ x }}") and a.has_template(["a", {"k": "{% if %}"}]) and a.has_template({"{{k}}": 1}) and not a.has_template("plain {text}")
    assert a._duration("00:05:00", "d") == "00:05:00" and a._duration("01:30", "d") == "01:30:00" and a._duration(90, "d") == "00:01:30"
    for bad in (True, "5", "99", -1, 100_000, None, "1:2:3:4"):
        with pytest.raises(a.DefinitionError):
            a._duration(bad, "d")
    for bad in (True, float("nan"), float("inf"), "5", 10**10, None):
        with pytest.raises(a.DefinitionError):
            a._number(bad, "n")
    assert a.definition_problem({"a": ["{{ x }}"]}, lambda t: False) == "templates" and a.definition_problem({"a": "lock.x"}, lambda t: t.startswith("lock")) == "hidden"
    assert a.definition_problem({"a": "fine"}, lambda t: False) is None
