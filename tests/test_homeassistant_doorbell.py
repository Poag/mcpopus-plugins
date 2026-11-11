"""A doorbell chime automation: an event entity as the trigger, an input_boolean helper as the target. The owner-reported failure of 11 November 2026.

The read filter (entity_allowlist) hides everything it does not match, the write filter (write_entity_allowlist) hides nothing, and the definition
messages say which rule blocked an id without revealing whether a hidden entity exists.
"""

import re

from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import st
from test_homeassistant import HOME, SECRETS, body, call, ha, values  # noqa: F401  (ha is a fixture)

ON = Policy(read_only=False, enable_tools=["save_automation", "delete_automation"])
CHIME = {"automation_id": "claude_doorbell_chime", "alias": "Doorbell chime",
         "triggers": [{"type": "state", "entity_id": "event.doorbell_doorbell"}],
         "actions": [{"service": "input_boolean.turn_on", "entity_id": "input_boolean.doorbell_chime"}, {"delay": "00:00:03"},
                     {"service": "input_boolean.turn_off", "entity_id": "input_boolean.doorbell_chime"}]}
SERVICES = ["input_boolean.turn_on", "input_boolean.turn_off"]


def with_doorbell(ha, helper=True):
    ha.states["event.doorbell_doorbell"] = st("event.doorbell_doorbell", "2026-11-11T07:55:01.123+00:00", friendly_name="Doorbell", event_type="pressed", event_types=["pressed"])
    if helper:
        ha.states["input_boolean.doorbell_chime"] = st("input_boolean.doorbell_chime", "off", friendly_name="Doorbell chime")


def cfg(ha, **kw):
    return values(ha, allowed_services=SERVICES, write_entity_allowlist=["input_boolean.doorbell_chime"], **kw)


async def test_the_doorbell_chime_is_saved_disabled_with_an_event_entity_trigger(ha, tmp_path):
    with_doorbell(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await call(c, "save_automation", CHIME)
        assert not err and "Created automation claude_doorbell_chime" in body(t) and "starts DISABLED" in body(t), t
    saved = ha.saved[0]
    assert saved["initial_state"] is False and saved["triggers"] == [{"trigger": "state", "entity_id": "event.doorbell_doorbell"}]  # no `to`: an event entity's state is a timestamp, never "on"
    assert saved["actions"] == [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.doorbell_chime"}}, {"delay": "00:00:03"},
                                {"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.doorbell_chime"}}]
    assert ha.states["automation.doorbell_chime"]["state"] == "off" and ha.service_calls == []  # saved, not enabled, nothing run


async def test_an_allow_list_naming_only_the_helper_hides_the_trigger_and_the_message_says_so(ha, tmp_path):
    """The reported mistake: the helper went into the READ allow list to make it writable, which hides every other entity."""
    with_doorbell(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, entity_allowlist=["input_boolean.doorbell_chime"]), SECRETS, policy=ON) as (c, _, _):
        err, t = await call(c, "save_automation", CHIME)
        assert err and "event.doorbell_doorbell in a state trigger cannot be used" in t and "outside the entity_allowlist setting" in t and "read filter" in t, t
        assert "input_boolean.doorbell_chime" not in t.split("outside")[0]  # the helper itself is visible, so it is not what was refused
        status = (await call(c, "status"))[1]
        assert "Visible entities: 1" in status and "Allow list note" in status and "write_entity_allowlist" in status and "WARNING" not in status
    assert ha.saved == []


async def test_an_allow_list_that_matches_nothing_gives_zero_visible_and_a_warning(ha, tmp_path):
    with_doorbell(ha, helper=False)  # the helper does not exist yet, so a list naming only it matches nothing
    async with running_plugin(tmp_path, HOME, cfg(ha, entity_allowlist=["input_boolean.doorbell_chime"]), SECRETS, policy=ON) as (c, _, _):
        status = (await call(c, "status"))[1]
        assert "Visible entities: 0" in status and "1 allow pattern(s)" in status and "matches no entity at all" in status and "entity_allowlist" in status
        assert "Write note: write_entity_allowlist is set but no visible entity matches" in status


async def test_naming_both_entities_in_the_allow_list_makes_both_usable(ha, tmp_path):
    with_doorbell(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha, entity_allowlist=["event.doorbell_doorbell", "input_boolean.doorbell_chime"]), SECRETS, policy=ON) as (c, _, _):
        status = (await call(c, "status"))[1]
        assert "Visible entities: 2" in status and "1 visible entity(ies) match those patterns" in status and "WARNING" not in status
        assert not (await call(c, "save_automation", CHIME))[0]


async def test_the_write_list_hides_nothing_and_the_helper_must_match_it(ha, tmp_path):
    with_doorbell(ha)
    async with running_plugin(tmp_path, HOME, values(ha, allowed_services=SERVICES, write_entity_allowlist=["light.*"]), SECRETS, policy=ON) as (c, _, _):
        status = (await call(c, "status"))[1]
        assert "event.doorbell_doorbell" not in status and "input_boolean 1" in status and "event 1" in status  # visible whatever the write list says
        err, t = await call(c, "save_automation", CHIME)
        assert err and "input_boolean.doorbell_chime is not on the write list" in t
    assert ha.saved == []


async def test_each_message_names_the_rule_and_a_hidden_entity_looks_like_a_missing_one_behind_the_same_rule(ha, tmp_path):
    with_doorbell(ha)
    ha.states["sensor.secret_meter"] = st("sensor.secret_meter", "5", friendly_name="Secret")
    async with running_plugin(tmp_path, HOME, cfg(ha, entity_denylist=["sensor.secret_*"]), SECRETS, policy=ON) as (c, _, _):
        async def refusal(eid):
            err, t = await call(c, "save_automation", {**CHIME, "triggers": [{"type": "state", "entity_id": eid}]})
            assert err
            return t
        domain_hidden, domain_missing = await refusal("lock.front_door"), await refusal("lock.no_such_lock")  # exists, and does not exist
        deny_hidden, deny_missing = await refusal("sensor.secret_meter"), await refusal("sensor.secret_nothing")
        absent = await refusal("sensor.not_real")
        norm = lambda t: re.sub(r"(lock|sensor)\.[a-z_]+", "ID", t)  # noqa: E731
        assert "domain (lock) is in the domain_denylist setting" in domain_hidden and norm(domain_hidden) == norm(domain_missing)  # existence is not revealed
        assert "matches the entity_denylist setting" in deny_hidden and norm(deny_hidden) == norm(deny_missing)
        assert "does not exist in Home Assistant" in absent and "denylist" not in absent and "entity_allowlist" not in absent
    assert ha.saved == []


async def test_the_missing_helper_is_reported_as_missing(ha, tmp_path):
    with_doorbell(ha, helper=False)
    async with running_plugin(tmp_path, HOME, cfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await call(c, "save_automation", CHIME)
        assert err and "input_boolean.doorbell_chime in the input_boolean.turn_on action does not exist in Home Assistant" in t
    assert ha.saved == []


async def test_a_trigger_that_waits_for_on_is_accepted_but_the_docs_say_it_never_fires_for_an_event_entity(ha, tmp_path):
    """The plugin cannot know an entity's kind of state, so `to: on` is accepted; the design and the tool text steer away from it."""
    with_doorbell(ha)
    async with running_plugin(tmp_path, HOME, cfg(ha), SECRETS, policy=ON) as (c, _, _):
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "event" in tools["save_automation"].description and "no \"to\"" in tools["save_automation"].description
