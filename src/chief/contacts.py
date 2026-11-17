"""Owner-curated handle -> display name roster (``<data_dir>/contacts.yaml``).

Channels only carry raw handles (a phone number, an email). Monitor wakes add
the roster name beside the handle so a session can tell who is talking. The
handle stays the identity everywhere (scoping, owner checks); the name is a
label only, and it comes from a file chief and the owner write, never from the
message itself. Read on every lookup so an edit applies without a restart.
"""

import logging
from pathlib import Path

import yaml

from chief.policy import normalize_handle

logger = logging.getLogger(__name__)


class Roster:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        try:
            raw = yaml.safe_load(self._path.read_text()) or {}
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError):
            logger.exception("contacts roster %s unreadable", self._path)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            normalize_handle(str(k)): str(v).strip()
            for k, v in raw.items()
            if v is not None and str(v).strip()
        }

    def name_for(self, handle: str) -> str | None:
        if not handle:
            return None
        return self._load().get(normalize_handle(handle))
