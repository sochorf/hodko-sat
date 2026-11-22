"""Select exactly one controller without mutating an existing installation.

Pre-SAT5 entries retain their 4.2.1.51 adapter and version 10 schema. This
deliberately avoids SAT5 identifier/preset migration, room writes and DHW control
for those entries. New standard entries use the SAT5 v13 schema. An update of
the package is not permission to change the user's heating policy.
"""


def uses_compat(entry) -> bool:
    """Saved Hodko modes always win, including shadow created by the new flow."""
    return (
        entry.version <= 10
        or entry.data.get('mode') == 'ally_shadow'
        or 'hodko_control_mode' in entry.options
        or 'hodko_ally' in entry.options
    )


def assert_no_compat_owner(hass, entry) -> None:
    """Do not let a new SAT5 entry take an already configured legacy gateway.

    Check configured ownership, not just a loaded instance: a startup retry must
    not briefly free the gateway. Shadow and explicitly disabled entries do not
    own a boiler. Existing controllers are never stopped by this guard.
    """
    from homeassistant.exceptions import ConfigEntryError

    device = entry.data.get('device')
    if not device or entry.data.get('simulation'):
        return
    for other in hass.config_entries.async_entries('sat'):
        if (other.entry_id == entry.entry_id or other.disabled_by is not None
                or other.source == 'ignore' or not uses_compat(other)
                or other.data.get('mode') == 'ally_shadow'
                or other.data.get('simulation')):
            continue
        if other.data.get('device') == device:
            raise ConfigEntryError('Gateway already belongs to a Hodko compatibility controller')
