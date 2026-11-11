"""create_helper: one input_boolean, named with the owner's prefix, put in the owner's helper category. Design section 9d."""

import logging

import pytest
from mcpopus.pluginconfig import Policy

from conftest import running_plugin
from fake_ha import TOKEN, st
from test_homeassistant import HOME, SECRETS, call, ha, values  # noqa: F401  (ha is a fixture)

ON = Policy(read_only=False, enable_tools=["create_helper"])


def hcfg(ha, **kw):
    return values(ha, **{"helper_category": "Claude", **kw})


async def create(c, helper_id="doorbell_chime", **kw):
    return await call(c, "create_helper", {"helper_id": helper_id, **kw})


def created(ha):
    return [(c["name"], c.get("initial")) for c in ha.helper_create_calls]


async def test_off_until_ticked_by_name_and_declared_as_a_write_tool(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, hcfg(ha), SECRETS) as (c, lp, _):
        assert "create_helper" not in [t.name for t in (await c.list_tools()).tools]
        assert (await create(c))[0]
    async with running_plugin(tmp_path / "b", HOME, hcfg(ha), SECRETS, policy=Policy(read_only=False, enable_tools=["save_automation"])) as (c, lp, _):
        assert (await create(c))[0]  # each tool is enabled by its own name
    assert ha.helper_create_calls == [] and not any("input_boolean/create" in w for w in ha.writes)


async def test_creates_the_prefixed_helper_in_the_category_and_changes_nothing_else(ha, tmp_path):
    before = {k: dict(v) for k, v in ha.states.items()}
    async with running_plugin(tmp_path, HOME, hcfg(ha, write_entity_allowlist=["input_boolean.claude_*"]), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, "Doorbell_Chime")  # lower-cased
        assert not err and "Created the helper input_boolean.claude_doorbell_chime (off to start)." in t and "helper category Claude" in t and "matches write_entity_allowlist" in t, t
        _, status = await call(c, "status")
        assert "input_boolean helpers named input_boolean.claude_<name>, in the helper category Claude" in status
        err, t = await call(c, "get_entity", {"entity_id": "input_boolean.claude_doorbell_chime"})
        assert not err and "state: off" in t  # the caches were dropped, so the new helper is visible at once
    assert created(ha) == [("claude_doorbell_chime", False)]
    entry = next(e for e in ha.reg_entities if e["entity_id"] == "input_boolean.claude_doorbell_chime")
    assert entry["categories"] == {"helpers": "cat_helpers_claude"}  # only the helpers scope is touched
    assert ha.writes == ["WS input_boolean/create", "WS config/entity_registry/update"] and ha.service_calls == [] and ha.saved == []
    assert {k: v for k, v in ha.states.items() if k in before} == before  # no existing entity changed


async def test_initial_on_and_the_write_list_note(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, "porch_flag", initial=True)
        assert not err and "(on to start)" in t and "does not match write_entity_allowlist" in t and "input_boolean.claude_*" in t
    assert created(ha) == [("claude_porch_flag", True)]


async def test_a_custom_prefix_and_case_insensitive_category(ha, tmp_path):
    async with running_plugin(tmp_path, HOME, hcfg(ha, helper_id_prefix="bot_", helper_category="  claude  "), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert not err and "input_boolean.bot_doorbell_chime" in t
    assert created(ha) == [("bot_doorbell_chime", False)]


@pytest.mark.parametrize("bad", ["", "  ", "a b", "a__b", "_a", "a_", "x" * 41, "../x", "{{ x }}", "light.kitchen", "ü", "a-b", "a\nb"])
async def test_bad_ids_are_refused_before_anything_is_sent(ha, tmp_path, bad):
    async with running_plugin(tmp_path, HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, bad)
        assert err and "helper_id is 1 to 40" in t
    assert ha.helper_create_calls == [] and ha.writes == [] and ha.ws_attempts == 0


async def test_the_category_setting_is_required_and_the_category_must_exist(ha, tmp_path):
    async with running_plugin(tmp_path / "a", HOME, values(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and "not set up" in t and "has not named a helper category" in t and "Claude" not in t
        assert ha.ws_attempts == 0  # refused before any connection
    async with running_plugin(tmp_path / "b", HOME, hcfg(ha, helper_category="No such category"), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and "No helper category" in t and "nothing was created" in t and "Other" not in t  # other category names are not listed
    ha.categories_supported = False
    async with running_plugin(tmp_path / "c", HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and "nothing was created" in t
    assert ha.helper_create_calls == [] and ha.writes == []


async def test_existing_entities_are_not_overwritten_and_hidden_ids_look_alike(ha, tmp_path):
    ha.states["input_boolean.claude_taken"] = st("input_boolean.claude_taken", "on", friendly_name="Taken")
    ha.states["input_boolean.claude_secret_flag"] = st("input_boolean.claude_secret_flag", "on", friendly_name="Secret")
    async with running_plugin(tmp_path, HOME, hcfg(ha, entity_denylist=["input_boolean.claude_secret_*"]), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, "taken")
        assert err and "already exists, or cannot be created" in t
        (e1, hidden_existing), (e2, hidden_made_up) = await create(c, "secret_flag"), await create(c, "secret_other")
        assert e1 and e2 and "entity_denylist" in hidden_existing
        assert hidden_existing.replace("secret_flag", "X") == hidden_made_up.replace("secret_other", "X")  # existence behind a rule is not revealed
    assert ha.helper_create_calls == []


@pytest.mark.parametrize("settings, rule", [({"entity_allowlist": ["light.*"]}, "entity_allowlist"), ({"entity_denylist": ["input_boolean.claude_*"]}, "entity_denylist"),
                                            ({"domain_denylist": ["input_boolean"]}, "domain_denylist")])
async def test_a_helper_the_policy_would_hide_is_not_created_and_the_rule_is_named(ha, tmp_path, settings, rule):
    async with running_plugin(tmp_path, HOME, hcfg(ha, **settings), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and rule in t and "Nothing was created" in t and "Claude could not see or use it" in t
    assert ha.helper_create_calls == [] and ha.writes == []


async def test_a_token_whose_user_is_not_an_administrator_cannot_create_and_is_paused(ha, tmp_path):
    ha.token_is_admin = False
    async with running_plugin(tmp_path, HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and "refused the helper command" in t and "administrator" in t and "Everything else still works" in t and TOKEN not in t
        attempts = ha.ws_attempts
        err, t = await create(c, "other_flag")
        assert err and "paused" in t and ha.ws_attempts == attempts  # no second attempt while paused
        assert "access token works" in (await call(c, "status"))[1]  # everything else is unaffected
    assert ha.helper_create_calls == []


async def test_a_slow_registry_is_waited_for_and_a_missing_entry_is_reported_plainly(ha, tmp_path):
    ha.helper_registry_misses = 2
    async with running_plugin(tmp_path / "a", HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert not err and "helper category Claude" in t
    ha.helper_registry_misses = 99
    async with running_plugin(tmp_path / "b", HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, "second_flag")
        assert not err and "Created the helper input_boolean.claude_second_flag" in t and "not in a category" in t and "Assign the category in Home Assistant" in t


async def test_a_refused_category_assignment_and_a_different_id_are_reported_not_hidden(ha, tmp_path):
    ha.helper_update_fails = True
    async with running_plugin(tmp_path / "a", HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert not err and "would not put it in the helper category" in t
    ha.helper_update_fails, ha.helper_force_suffix = False, True
    async with running_plugin(tmp_path / "b", HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c, "clash_flag")
        assert not err and "Home Assistant named it input_boolean.claude_clash_flag_2, not input_boolean.claude_clash_flag" in t


async def test_home_assistant_rejecting_the_create_creates_nothing_and_its_text_is_not_repeated(ha, tmp_path):
    ha.helper_create_error = "invalid_format"
    async with running_plugin(tmp_path, HOME, hcfg(ha), SECRETS, policy=ON) as (c, _, _):
        err, t = await create(c)
        assert err and "would not create the helper" in t and "invalid_format" not in t and "Unauthorized" not in t
    assert ha.helper_create_calls == []


async def test_the_write_rate_limit_is_shared_and_the_token_never_appears(ha, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with running_plugin(tmp_path, HOME, hcfg(ha, max_writes_per_minute=1), SECRETS, policy=ON) as (c, _, _):
        out = [await create(c, "first_flag"), await create(c, "second_flag")]
        assert not out[0][0] and out[1][0] and "max_writes_per_minute" in out[1][1]
    assert created(ha) == [("claude_first_flag", False)]
    assert TOKEN not in "\n".join(t for _, t in out) and TOKEN not in caplog.text and TOKEN not in (tmp_path / "audit.jsonl").read_text()


async def test_config_rules(ha, tmp_path):
    from mcpopus.manager import PluginLoadError

    for field, bad in (("helper_id_prefix", "Bad Prefix"), ("helper_id_prefix", ""), ("helper_id_prefix", "x" * 21), ("helper_id_prefix", "a__b"), ("helper_id_prefix", "1abc"),
                       ("helper_category", "x" * 81), ("helper_category", "a\x07b")):
        with pytest.raises(PluginLoadError) as e:
            async with running_plugin(tmp_path / (field + str(len(bad)) + bad[:1].encode().hex()), HOME, values(ha, **{field: bad}), SECRETS):
                pass
        assert field in str(e.value) and (not bad.strip() or bad not in str(e.value))
