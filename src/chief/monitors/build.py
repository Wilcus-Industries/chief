"""Build the monitor service from config: roster, thread history, cursors.

Kept out of app.py: which channels have a record a wake can read (today only
iMessage's ``chat.db``), and where the per-monitor cursors live, are monitor
concerns, not app wiring.
"""

from chief.adapters.imessage import BOT_PREFIX
from chief.adapters.imessage_attachments import ImageStash
from chief.adapters.imessage_history import HistoryStore, IMessageHistory
from chief.bus import EventBus
from chief.classifiers import Classifier
from chief.config import Config
from chief.contacts import Roster
from chief.monitors.history import HistorySource, SeenCursors
from chief.monitors.service import MonitorService, WakeAgent
from chief.persistence.db import SessionFactory
from chief.screen import Screen

CURSORS_FILE = "monitor_seen.json"
PHOTOS_DIR = "attachments/imessage"  # under the data dir


def photo_stash(config: Config) -> ImageStash:
    """The one place the owner's iMessage photos are kept (adapter + wakes)."""
    return ImageStash(config.db_path.parent / PHOTOS_DIR)


def build_monitors(
    config: Config, factory: SessionFactory, bus: EventBus, wake: WakeAgent,
    classifier: Classifier, screen: Screen | None,
) -> MonitorService:
    data = config.db_path.parent
    return MonitorService(
        factory, bus, wake, classifier, screen, config.imessage_owner_handles,
        Roster(data / "contacts.yaml"), histories(config),
        SeenCursors(data / CURSORS_FILE),
    )


def histories(config: Config) -> dict[str, HistorySource]:
    """Each channel's thread record, by channel name."""
    if not config.imessage_enabled:
        return {}
    if config.imessage_dedicated:
        stores = [HistoryStore(config.imessage_db_path, "chief")]
        if config.imessage_owner_db_path is not None:
            stores.append(HistoryStore(config.imessage_owner_db_path, "owner"))
    else:
        stores = [HistoryStore(config.imessage_db_path, "prefixed")]
    return {"imessage": IMessageHistory(
        stores, config.imessage_owner_handles, BOT_PREFIX, photo_stash(config))}
