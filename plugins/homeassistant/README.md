# homeassistant

Read the state of a Home Assistant home: search entities, read states and attributes, history and logbook for one entity, areas, and
the services you have allowed. Writes are limited to `call_service`, one allowed service on one named, allowed entity, off until the owner enables it and names what it may do. Entity and domain policy is enforced by the plugin, and a hidden entity is
indistinguishable from a missing one.

- Design, verified facts, policy and threat notes: [docs/DESIGN-homeassistant.md](../../docs/DESIGN-homeassistant.md)
- Live acceptance checklists and the settings to enter: sections 10, 10b, 10c and 10d of the design
- Settings are the `Config` class in `__init__.py`; the token is stored encrypted by the host.

Automations can be listed, triggered (conditions respected), enabled and disabled when they are in a Home Assistant category the owner names.

If the token's user is an administrator (use a dedicated one, marked local access only in Home Assistant) it can also read automation definitions and create, replace or delete automations whose id starts with a prefix (default `claude_`), within a strict subset and starting disabled.

Everything it returns came from Home Assistant, so it is marked untrusted. Create the access token on a dedicated non-administrator
Home Assistant user, and revoke it there to cut access. 
## Packages

`plugin.toml` lists `wsproto==1.3.2` and `h11==0.16.0` (hashed, wheels only) under `requirements`, and `min_host_version = "0.3.0"`. The host installs
them when you add or update the plugin (after you tick the trust box) or from the plugin's page. They are used for the WebSocket that reads areas,
devices and automation categories (httpx2's WebSocket client imports `wsproto`). If they are missing the plugin does not load; if the import still
fails, area data says "wsproto is missing: install the plugin's packages from its page" and everything else works.
