"""The refusal cases of the live checklist (design section 10f) replayed against a fake that mirrors Alex's home, with the settings the checklist asks for.

An earlier live run proved only seven of ten rules because three cases were stopped by an earlier rule. This test pins, for every case in the prompt, the
message it must reach, so the prompt cannot drift from the code unnoticed. Names are the real ones looked up on 10 November 2026.
"""

import pytest
from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import TOKEN, st
from test_homeassistant import HOME, SECRETS, call, ha, values  # noqa: F401  (ha is a fixture)

ALL = Policy(read_only=False, enable_tools=["save_automation", "delete_automation", "trigger_automation", "set_automation_enabled"])
BASE = {"automation_id": "claude_refusal_test", "alias": "Claude refusal test",
        "triggers": [{"type": "state", "entity_id": "binary_sensor.ev6_back_left_door", "to": "on"}],
        "actions": [{"service": "light.turn_on", "entity_id": "light.outside_porch"}]}
REAL_NAME = "Inside porch light - day, evening, night"


def live_home(ha):
    for e in ("light.kitchen", "light.hall", "light.bedroom"):
        ha.states.pop(e)
    ha.states.update({s["entity_id"]: s for s in (
        st("binary_sensor.ev6_back_left_door", "off", friendly_name="EV6 Back Left Door", device_class="door"),
        st("light.outside_porch", "off", friendly_name="Outside Porch"), st("light.inside_porch", "on", friendly_name="Inside Porch"),
        st("light.lights_red_light", "off", friendly_name="Red Light"), st("light.nursery_light", "unavailable", friendly_name="Nursery Light"),
        st("switch.ev6_charge_port", "off", friendly_name="EV6 Charge Port"), st("person.vicki", "home", friendly_name="Vicki"),
        st("automation.inside_porch_light_day_evening_night", "on", friendly_name=REAL_NAME, id="1700000000001", last_triggered="2026-11-10T06:33:42+00:00", current=0),
        st("automation.hoover_at_11am_when_house_is_quiet", "on", friendly_name="Hoover at 11am when house is quiet", id="1700000000002", last_triggered="2026-11-10T11:00:00+00:00", current=0),
    )})
    for e in [e for e in ha.states if e.startswith("automation.") and e not in ("automation.inside_porch_light_day_evening_night", "automation.hoover_at_11am_when_house_is_quiet")]:
        ha.states.pop(e)
    ha.categories = [{"category_id": "cat_claude", "name": "Claude"}, {"category_id": "cat_lights", "name": "Lights"}, {"category_id": "cat_hoover", "name": "Hoover"}]
    ha.reg_entities += [{"entity_id": "automation.inside_porch_light_day_evening_night", "area_id": None, "device_id": None, "categories": {"automation": "cat_lights"}},
                        {"entity_id": "automation.hoover_at_11am_when_house_is_quiet", "area_id": None, "device_id": None, "categories": {"automation": "cat_hoover"}}]
    ha.automation_configs = {"1700000000001": {"id": "1700000000001", "alias": REAL_NAME, "triggers": [{"trigger": "time", "at": "06:30:00"}],
                                               "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.inside_porch"}}]}}


def live_cfg(ha):
    return values(ha, allowed_services=["light.turn_on", "light.turn_off"], write_entity_allowlist=["light.outside_porch", "light.inside_porch"], automation_categories=["claude"])


def swap(base, **kw):
    return {**base, **kw}


def action(**kw):
    return [{"service": "light.turn_on", "entity_id": "light.outside_porch", **kw}]


SAVE_CASES = [  # (step in the prompt, changes to the base definition, text the refusal must contain)
    (5, {"automation_id": "porch_refusal_test"}, "must start with claude_"),
    (7, {"actions": [{"service": "light.toggle", "entity_id": "light.outside_porch"}]}, "allowed_services"),
    (8, {"actions": [{"service": "lock.unlock", "entity_id": "lock.front_door"}]}, "allowed_services"),
    (9, {"actions": action(entity_id="person.vicki")}, "person.vicki in the light.turn_on action cannot be used: its domain (person) is in the domain_denylist"),
    (10, {"actions": action(entity_id="light.does_not_exist")}, "light.does_not_exist in the light.turn_on action does not exist in Home Assistant"),
    (11, {"actions": action(entity_id="light.lights_red_light")}, "not on the write list"),
    (12, {"actions": action(entity_id="switch.ev6_charge_port")}, "same domain"),
    (13, {"actions": action(data={"brightness": "{{ 50 }}"})}, "template"),
    (14, {"actions": action(data={"transition": "light.inside_porch"})}, "other entities"),
    (15, {"actions": action(data={"area_id": "lights"})}, "may not name targets"),
    (16, {"triggers": [{"type": "state", "entity_id": "light.outside_porch", "to": "on"}]}, "trigger itself"),
    (17, {"triggers": [{"type": "event", "event_type": "call_service"}]}, "Each trigger needs a type"),
    (18, {"actions": [{"delay": "02:00:00"}, *action()]}, "longer than allowed"),
    (19, {"conditions": [{"type": "template", "value_template": "{{ true }}"}]}, "Each condition needs a type"),
    (20, {"automation_id": "claude_refusal_name", "alias": REAL_NAME}, "already exists"),
]


async def test_every_refusal_case_of_the_live_prompt_reaches_the_rule_it_names(ha, tmp_path):
    live_home(ha)
    async with running_plugin(tmp_path, HOME, live_cfg(ha), SECRETS, policy=ALL) as (c, _, _):
        for step, change, expect in SAVE_CASES:
            err, text = await call(c, "save_automation", swap(BASE, **change))
            assert err and expect in text, (step, text[:300])
        err, text = await call(c, "delete_automation", {"automation_id": "inside_porch_light_day_evening_night"})  # step 6
        assert err and "must start with claude_" in text and "No such automation" not in text
        err, text = await call(c, "get_entity", {"entity_id": "person.vicki"})
        assert err or "No such entity" in text
    assert ha.saved == [] and not any(m != "GET" for m, _ in ha.config_calls) and ha.service_calls == [] and ha.writes == []  # nothing reached Home Assistant


async def test_the_base_definition_of_the_live_prompt_is_itself_valid(ha, tmp_path):
    """If the base were refused, every refusal above would pass for the wrong reason."""
    live_home(ha)
    async with running_plugin(tmp_path, HOME, live_cfg(ha), SECRETS, policy=ALL) as (c, _, _):
        err, text = await call(c, "save_automation", BASE)
        assert not err and "Created automation claude_refusal_test" in text and "DISABLED" in text, text
        # step 22's definition and step 29's condition are valid too
        full = swap(BASE, automation_id="claude_porch_test", alias="Claude porch test", description="Live test, safe to delete", mode="single",
                    actions=[*action(data={"brightness": 128}), {"delay": "00:00:10"}, {"service": "light.turn_off", "entity_id": "light.outside_porch"}],
                    conditions=[{"type": "state", "entity_id": "binary_sensor.ev6_back_left_door", "state": "on"}])
        err, text = await call(c, "save_automation", full)
        assert not err and "Created automation claude_porch_test" in text, text
    assert [s["id"] for s in ha.saved] == ["claude_refusal_test", "claude_porch_test"] and all(s["initial_state"] is False for s in ha.saved)


async def test_a_claude_id_is_not_permission_to_run_an_automation_but_the_category_is(ha, tmp_path):
    live_home(ha)
    async with running_plugin(tmp_path, HOME, live_cfg(ha), SECRETS, policy=ALL) as (c, _, _):
        assert not (await call(c, "save_automation", swap(BASE, automation_id="claude_uncategorised_test", alias="Claude uncategorised test")))[0]
        entity = "automation.claude_uncategorised_test"
        _, text = await call(c, "list_automations")
        assert f"{entity} | Claude uncategorised test | off" in text and text.count("controllable: no") == 3 and "controllable: yes" not in text
        for tool, args in (("trigger_automation", {"entity_id": entity}), ("set_automation_enabled", {"entity_id": entity, "enabled": True}),
                           ("set_automation_enabled", {"entity_id": "automation.hoover_at_11am_when_house_is_quiet", "enabled": True})):
            err, text = await call(c, tool, args)
            assert err and "may not control that automation" in text, (tool, text)
        err, text = await call(c, "trigger_automation", {"entity_id": "automation.does_not_exist"})
        assert err and "No such entity." in text
        err, text = await call(c, "set_automation_enabled", {"entity_id": "light.outside_porch", "enabled": True})
        assert err and "must be an automation" in text
    assert ha.service_calls == [] and TOKEN not in text  # no automation service was ever sent
