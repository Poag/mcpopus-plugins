"""One prefix (claude_prefix) and one category (claude_category), used by every area. Design section 5b.

Also the move from the four old settings (automation_id_prefix, automation_categories, helper_id_prefix, helper_category): an update must not silently drop a stored value.
"""

import sys

import pytest
from mcpopus.manager import PluginLoadError
from mcpopus.pluginconfig import Policy

from conftest import plugin_module, running_plugin
from test_homeassistant import HOME, SECRETS, call, ha, values  # noqa: F401  (ha is a fixture)

ALL = Policy(read_only=False, enable_tools=["save_automation", "delete_automation", "create_helper", "trigger_automation", "set_automation_enabled"])
AUTO = {"alias": "Porch when door opens", "triggers": [{"type": "state", "entity_id": "binary_sensor.door_contact", "to": "on"}], "actions": [{"service": "light.turn_on", "entity_id": "light.kitchen"}]}


def cfg(ha, **kw):
    return values(ha, allowed_services=["light.turn_on", "light.turn_off"], write_entity_allowlist=["light.*"], **kw)


async def test_one_category_and_one_prefix_drive_automations_and_helpers_alike(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, cfg(ha, claude_category="Claude", claude_prefix="bot_"), SECRETS, policy=ALL) as (c, _, _):
        _, t = await call(c, "list_automations")
        assert "automation.porch_evening | Porch Evening | on" in t and "category: Claude | controllable: yes" in t and "garage_night | Garage Night | on | last run never | running 0 | category: Other | controllable: no" in t
        assert not (await call(c, "trigger_automation", {"entity_id": "automation.porch_evening"}))[0]  # the automation category
        err, t = await call(c, "create_helper", {"helper_id": "doorbell_chime"})
        assert not err and "input_boolean.bot_doorbell_chime" in t and "It is in the category Claude" in t  # the helper category: the same name, the helpers scope
        assert not (await call(c, "save_automation", {**AUTO, "automation_id": "bot_porch"}))[0]
        err, t = await call(c, "save_automation", {**AUTO, "automation_id": "claude_porch", "alias": "Another"})
        assert err and "must start with bot_" in t  # the same prefix
        _, status = await call(c, "status")
        assert "ids Claude may create or change start with bot_" in status and "category Claude" in status and "named input_boolean.bot_<name>, in the category Claude" in status
        assert "Old settings" not in status
    assert ha.reg_entities[-1]["categories"] == {"helpers": "cat_helpers_claude"}
    assert ha.helper_create_calls[0]["name"] == "bot_doorbell_chime" and ha.saved[0]["id"] == "bot_porch"


async def test_an_empty_category_means_nothing_is_controllable_by_category_and_no_helper(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, cfg(ha), SECRETS, policy=ALL) as (c, _, _):
        _, t = await call(c, "list_automations")
        assert "controllable: yes" not in t.replace("porch_evening | Porch Evening | on | last run 2026-11-07T18:00:00+00:00 | running 0 | category: Claude | controllable: no", "")
        assert (await call(c, "trigger_automation", {"entity_id": "automation.porch_evening"}))[0]
        err, t = await call(c, "create_helper", {"helper_id": "x"})
        assert err and "claude_category" in t
        assert "no category set (claude_category is empty)" in (await call(c, "status"))[1]
    assert ha.service_calls == [] and ha.helper_create_calls == []


async def test_old_stored_settings_keep_working_and_status_asks_for_a_save(ha, tmp_path):
    old = cfg(ha, automation_categories=["Claude"], automation_id_prefix="bot_", helper_id_prefix="bot_", helper_category="claude")
    async with running_plugin(tmp_path, HOME, old, SECRETS, policy=ALL) as (c, _, _):
        _, t = await call(c, "list_automations")
        assert "porch_evening | Porch Evening | on | last run 2026-11-07T18:00:00+00:00 | running 0 | category: Claude | controllable: yes" in t
        err, t = await call(c, "create_helper", {"helper_id": "doorbell_chime"})
        assert not err and "input_boolean.bot_doorbell_chime" in t and "category Claude" in t
        assert not (await call(c, "save_automation", {**AUTO, "automation_id": "bot_porch"}))[0]
        _, status = await call(c, "status")
        assert "Old settings are still stored and are no longer used: automation_categories, automation_id_prefix, helper_category, helper_id_prefix" in status
        assert "claude_prefix and claude_category" in status and "bot_" not in status.split("Old settings")[1].split("Helpers Claude")[0]  # names, never values
    assert ha.helper_create_calls[0]["name"] == "bot_doorbell_chime"


async def test_a_new_value_wins_over_an_old_one_and_the_old_one_is_still_reported(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, cfg(ha, claude_category="Other", automation_categories=["Claude"]), SECRETS, policy=ALL) as (c, _, _):
        _, t = await call(c, "list_automations")
        assert "garage_night | Garage Night | on | last run never | running 0 | category: Other | controllable: yes" in t and "porch_evening | Porch Evening | on | last run 2026-11-07T18:00:00+00:00 | running 0 | category: Claude | controllable: no" in t
        assert "Old settings are still stored" in (await call(c, "status"))[1]


@pytest.mark.parametrize("extra, field", [({"automation_id_prefix": "a_", "helper_id_prefix": "b_"}, "claude_prefix"),
                                          ({"automation_categories": ["Claude"], "helper_category": "Other"}, "claude_category"),
                                          ({"automation_categories": ["Claude", "Other"]}, "claude_category")])
async def test_old_settings_that_disagree_refuse_to_load_and_name_the_new_setting(ha, tmp_path, extra, field):
    with pytest.raises(PluginLoadError) as e:
        async with running_plugin(tmp_path, HOME, cfg(ha, **extra), SECRETS):
            pass
    assert field in str(e.value) and "Other" not in str(e.value) and "a_" not in str(e.value)  # no values in the message


async def test_old_settings_that_agree_in_different_case_or_repeated_are_accepted(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, cfg(ha, automation_categories=["CLAUDE", "claude"], helper_category="Claude", automation_id_prefix="bot_", helper_id_prefix="bot_"), SECRETS, policy=ALL) as (c, _, _):
        assert "controllable: yes" in (await call(c, "list_automations"))[1]


async def test_the_form_has_only_the_two_new_settings(ha):
    plugin_module("automations", HOME)  # importing a submodule loads the package, which holds Config
    schema = sys.modules["_t_" + HOME].Config.model_json_schema()
    props = set(schema["properties"])
    assert {"claude_prefix", "claude_category"} <= props
    assert not props & {"automation_id_prefix", "automation_categories", "helper_id_prefix", "helper_category"}


async def test_status_says_whether_the_category_exists_for_automations_and_for_helpers(ha, tmp_path):
    """The reported failure: a category named Claude existed for automations only, and nothing said so."""
    async with running_plugin(tmp_path / "a", HOME, cfg(ha, claude_category="Claude"), SECRETS) as (c, _, _):
        t = (await call(c, "status"))[1]
        assert "Category Claude: for automations found, 2 automation(s) in it; for helpers found. These are two separate categories that share a name." in t
    ha.helper_categories = [{"category_id": "cat_helpers_other", "name": "Other"}]  # the category was only made for automations
    async with running_plugin(tmp_path / "b", HOME, cfg(ha, claude_category="claude"), SECRETS, policy=ALL) as (c, _, _):
        t = (await call(c, "status"))[1]
        assert "for automations found" in t and "for helpers NOT found (create_helper will create it the first time it is used, or create it on the Helpers page" in t
        assert "Other" not in t  # other categories' names are not listed
        err, t = await call(c, "create_helper", {"helper_id": "doorbell_chime"})
        assert not err and "so it was created first" in t  # and now status finds it
        assert "for helpers found" in (await call(c, "status"))[1]
    ha.categories, ha.helper_categories = [{"category_id": "cat_other", "name": "Other"}], []
    async with running_plugin(tmp_path / "c", HOME, cfg(ha, claude_category="Claude"), SECRETS) as (c, _, _):
        t = (await call(c, "status"))[1]
        assert "for automations NOT found (create it under Settings, Automations, Categories); for helpers NOT found" in t
    ha.categories_supported = False
    async with running_plugin(tmp_path / "d", HOME, cfg(ha, claude_category="Claude"), SECRETS) as (c, _, _):
        assert "for automations could not be checked; for helpers could not be checked" in (await call(c, "status"))[1]  # unknown is not "missing"
    assert len(ha.helper_create_calls) == 1


async def test_status_has_no_category_line_when_none_is_set_and_a_category_with_no_automation_is_said_so(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, cfg(ha), SECRETS) as (c, _, _):
        assert "Category " not in (await call(c, "status"))[1]
    ha.reg_entities[:] = [e for e in ha.reg_entities if not (e["entity_id"].startswith("automation.") and e.get("categories", {}).get("automation") == "cat_claude")]
    async with running_plugin(tmp_path / "b", HOME, cfg(ha, claude_category="Claude"), SECRETS) as (c, _, _):
        assert "for automations found, no automation is in it yet" in (await call(c, "status"))[1]
