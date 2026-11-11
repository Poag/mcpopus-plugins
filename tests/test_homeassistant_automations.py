"""Automation tools: listing, trigger and enable or disable, fenced by Home Assistant category."""

import pytest
from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import TOKEN
from test_homeassistant import HOME, SECRETS, body, call, ha, values  # noqa: F401  (ha is a fixture)

ON = Policy(read_only=False, enable_tools=["trigger_automation", "set_automation_enabled"])


def acfg(ha, **kw):
    return values(ha, claude_category="Claude", **kw)


async def trig(c, eid="automation.porch_evening"):
    return await call(c, "trigger_automation", {"entity_id": eid})


async def test_listing_shows_category_and_whether_it_is_controllable(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha, entity_denylist=["automation.secret_*"]), SECRETS) as (c, _, _):
        _, t = await call(c, "list_automations")
        assert "3 automations" in t and "secret" not in t.lower()  # hidden by the deny list
        assert "automation.porch_evening | Porch Evening | on | last run 2026-11-07T18:00:00+00:00 | running 0 | category: Claude | controllable: yes" in t
        assert "automation.garage_night | Garage Night | on | last run never | running 0 | category: Other | controllable: no" in t
        assert "automation.lock_up" in t and "lock_up | Lock Up | on | last run 2026-11-07T23:00:00+00:00 | running 0 | controllable: no" in t
        _, t = await call(c, "list_automations", {"category": "claude"})
        assert "1 automations" in t and "porch_evening" in t and "garage_night" not in t
        _, t = await call(c, "list_automations", {"state": "off"})
        assert "0 automations" in t
        _, t = await call(c, "list_automations", {"limit": 1, "offset": 1})
        assert "showing 2 to 2" in t and "Use offset 2" in t


async def test_off_until_enabled_by_name(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, acfg(ha), SECRETS) as (c, _, _):
        names = [t.name for t in (await c.list_tools()).tools]
        assert "list_automations" in names and "trigger_automation" not in names and "set_automation_enabled" not in names
        assert (await trig(c))[0] and (await call(c, "set_automation_enabled", {"entity_id": "automation.porch_evening", "enabled": False}))[0]
    async with running_plugin(tmp_path / "b", HOME, acfg(ha), SECRETS, policy=Policy(read_only=False, enable_tools=["trigger_automation"])) as (c, _, _):
        names = [t.name for t in (await c.list_tools()).tools]
        assert "trigger_automation" in names and "set_automation_enabled" not in names  # each is enabled by its own name
    assert ha.service_calls == [] and ha.writes == []


async def test_trigger_respects_conditions_and_reports_before_and_after(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await trig(c)
        text = body(t)
        assert not err and "Triggered automation.porch_evening (its conditions still apply)." in text
        assert "before: on, last run 2026-11-07T18:00:00+00:00, running 0" in text and "after: on, last run 2026-11-08T12:00:00+00:00" in text
    assert ha.service_calls == [("automation", "trigger", {"skip_condition": False, "entity_id": "automation.porch_evening"})]


async def test_enable_and_disable(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await call(c, "set_automation_enabled", {"entity_id": "automation.porch_evening", "enabled": False})
        assert not err and "after: off" in body(t) and "left to finish" in t
        err, t = await call(c, "set_automation_enabled", {"entity_id": "automation.porch_evening", "enabled": True})
        assert not err and "after: on" in body(t)
        err, t = await call(c, "set_automation_enabled", {"entity_id": "automation.porch_evening", "enabled": "maybe"})
        assert err
    assert [s[:2] for s in ha.service_calls] == [("automation", "turn_off"), ("automation", "turn_on")]
    assert ha.service_calls[0][2] == {"stop_actions": False, "entity_id": "automation.porch_evening"}  # never stops a run in progress


async def test_the_category_is_the_fence(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS, policy=ON) as (c, _, _):
        for eid in ("automation.garage_night", "automation.lock_up"):  # other category, and none
            err, t = await trig(c, eid)
            assert err and "may not control that automation" in t
            err, t = await call(c, "set_automation_enabled", {"entity_id": eid, "enabled": False})
            assert err and "may not control that automation" in t
        for bad in ("light.kitchen", "all", "automation.*", "automation.a,automation.b", ""):
            assert (await trig(c, bad))[0]
    async with running_plugin(tmp_path / "b", HOME, values(ha), SECRETS, policy=ON) as (c, _, _):  # no category set: nothing is controllable
        err, t = await trig(c)
        assert err and "may not control" in t
    async with running_plugin(tmp_path / "c", HOME, values(ha, write_entity_allowlist=["automation.garage_*"]), SECRETS, policy=ON) as (c, _, _):  # a write pattern also works
        assert not (await trig(c, "automation.garage_night"))[0]
        assert (await trig(c, "automation.porch_evening"))[0]
    assert [s[2]["entity_id"] for s in ha.service_calls] == ["automation.garage_night"]


async def test_hidden_and_missing_automations_look_alike(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha, entity_denylist=["automation.secret_*"]), SECRETS, policy=ON) as (c, _, _):
        missing = await trig(c, "automation.does_not_exist")
        assert missing[0] and "No such entity." in missing[1]
        got = await trig(c, "automation.secret_routine")
        assert got[0] == missing[0] and got[1].replace("secret_routine", "X") == missing[1].replace("does_not_exist", "X")
        got = await call(c, "set_automation_enabled", {"entity_id": "automation.secret_routine", "enabled": False})
        assert got[0] and "No such entity." in got[1]
    async with running_plugin(tmp_path / "b", HOME, acfg(ha, domain_denylist=["automation"]), SECRETS, policy=ON) as (c, _, _):
        assert "0 automations" in body((await call(c, "list_automations"))[1])
        assert "No such entity." in (await trig(c))[1]
    assert ha.service_calls == []


async def test_category_data_missing_is_explained_and_fails_closed(ha, tmp_path):
    ha.ws_enabled = False
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await trig(c)
        assert err and "Category information is not available" in t
        err, t = await call(c, "list_automations", {"category": "claude"})
        assert err and "needs category data" in t
        _, t = await call(c, "list_automations")
        assert "controllable: no" in t and "porch_evening" in t  # still lists, and says nothing is controllable
    assert ha.service_calls == []


async def test_older_home_assistant_without_categories_still_gives_areas(ha, tmp_path):
    ha.categories_supported = False
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS, policy=ON) as (c, _, _):
        assert "Area information: available" in (await call(c, "status"))[1]
        assert "may not control" in (await trig(c))[1]  # no categories known, so fail closed


async def test_automation_services_cannot_be_allowlisted_for_call_service(ha, tmp_path):
    from mcpopus.manager import PluginLoadError

    with pytest.raises(PluginLoadError) as e:
        async with running_plugin(tmp_path, HOME, values(ha, allowed_services=["automation.trigger"]), SECRETS):
            pass
    assert "allowed_services" in str(e.value) and "automation" in str(e.value)


async def test_rate_limit_is_shared_and_token_stays_secret(ha, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    async with running_plugin(tmp_path, HOME, acfg(ha, max_writes_per_minute=2), SECRETS, policy=ON) as (c, _, _):
        out = [await trig(c), await call(c, "set_automation_enabled", {"entity_id": "automation.porch_evening", "enabled": False}), await trig(c)]
        assert [o[0] for o in out] == [False, False, True] and "max_writes_per_minute" in out[2][1]
    assert len(ha.service_calls) == 2
    assert TOKEN not in "\n".join(o[1] for o in out) and TOKEN not in caplog.text and TOKEN not in (tmp_path / "audit.jsonl").read_text()


async def test_status_describes_the_automation_setup(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, acfg(ha), SECRETS) as (c, _, _):
        assert "category Claude" in (await call(c, "status"))[1]
    async with running_plugin(tmp_path / "b", HOME, values(ha), SECRETS) as (c, _, _):
        assert "no category set" in (await call(c, "status"))[1]
