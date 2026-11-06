# homeassistant

Read the state of a Home Assistant home: search entities, read states and attributes, history and logbook for one entity, areas, and
the services you have allowed. Read-only in this version. Entity and domain policy is enforced by the plugin, and a hidden entity is
indistinguishable from a missing one.

- Design, verified facts, policy and threat notes: [docs/DESIGN-homeassistant.md](../../docs/DESIGN-homeassistant.md)
- Live acceptance checklist and the settings to enter: section 10 of the design
- Settings are the `Config` class in `__init__.py`; the token is stored encrypted by the host.

Everything it returns came from Home Assistant, so it is marked untrusted. Create the access token on a dedicated non-administrator
Home Assistant user, and revoke it there to cut access. Area data needs the `wsproto` library in the host image; without it, everything
else still works.
