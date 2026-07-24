"""Chief's own update schedule, kept in step with ``update.schedule``.

Deliberately a **prompt** schedule, not a command one: the whole point is that
chief decides. A prompt row wakes it as an ordinary turn, so quiet hours are
respected and the restart lands at chief's own safe turn boundary — none of
which a headless command row would give.

Config is the source of truth. Chief may retune or delete the schedule through
the cron tool, but clearing ``update.schedule`` is what actually turns it off;
otherwise the next boot puts it back.
"""

import logging

from chief.config import Config
from chief.cron.service import CronService
from chief.cron.timing import validate_spec

logger = logging.getLogger(__name__)

#: How the row is recognised across boots. Changing it orphans existing rows.
UPDATE_DESCRIPTION = "core update check"

#: Says no autonomy level itself. The row is written once and never rewritten,
#: so a level named here would go on granting whatever it granted at seeding
#: time long after the owner dialled `update.autonomy` back.
_PROMPT = (
    "Time to check whether a newer core release is out. Load the "
    "`self-update` skill and follow it exactly. Read `update.autonomy` from "
    "config.yaml for what you may do unattended on this run — this schedule "
    "woke you, so no one is necessarily reading this thread."
)


def primary_target(config: Config) -> tuple[str, str]:
    """The owner's primary channel: the iMessage self-chat, else the web UI."""
    if config.imessage_enabled and config.imessage_owner_handles:
        return "imessage", config.imessage_owner_handles[0]
    return "web", "web:main"


async def ensure_update_schedule(
    cron: CronService, config: Config
) -> int | None:
    """Create the update schedule if config asks for one and none exists.

    Returns the new schedule id, or ``None`` when there was nothing to do.
    Never raises: a bad spec is the owner's typo, and it must not stop a boot.
    """
    if not config.update_schedule or config.update_autonomy == "off":
        return None
    try:
        validate_spec(config.update_schedule)
    except ValueError as exc:
        logger.error("update.schedule is not a valid cron spec: %s", exc)
        return None
    existing = await cron.list_enabled()
    if any(row.description == UPDATE_DESCRIPTION for row in existing):
        return None
    channel, thread = primary_target(config)
    schedule_id = await cron.create(
        description=UPDATE_DESCRIPTION,
        spec=config.update_schedule,
        wake_channel=channel,
        wake_thread=thread,
        prompt=_PROMPT,
    )
    logger.info(
        "update schedule #%s created (%s, waking %s)",
        schedule_id,
        config.update_schedule,
        thread,
    )
    return schedule_id
