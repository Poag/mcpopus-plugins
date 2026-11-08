# homeassistant

Read the state of a Home Assistant home: search entities, read states and attributes, history and logbook for one entity, areas, and
the services you have allowed. Writes are limited to `call_service`, one allowed service on one named, allowed entity, off until the owner enables it and names what it may do. Entity and domain policy is enforced by the plugin, and a hidden entity is
indistinguishable from a missing one.

- Design, verified facts, policy and threat notes: [docs/DESIGN-homeassistant.md](../../docs/DESIGN-homeassistant.md)
- Live acceptance checklists and the settings to enter: sections 10 and 10b of the design
- Settings are the `Config` class in `__init__.py`; the token is stored encrypted by the host.

Everything it returns came from Home Assistant, so it is marked untrusted. Create the access token on a dedicated non-administrator
Home Assistant user, and revoke it there to cut access. Area data needs the `wsproto` library in the host image; without it, everything
else still works.
