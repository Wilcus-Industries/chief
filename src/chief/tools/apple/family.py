"""Boot gating for the Apple tool family (#155): probes decide what registers.

Darwin detection lives in config (``Settings.apple_configured`` — enabled flag on by
default, effective only on macOS, force-off available); this module owns the second
gate: at boot, :func:`~chief.tools.apple.doctor.probe_all` checks each capability's
TCC grant and :meth:`AppleToolFamily.build_services` registers **only** the app-area
services whose probe passed — a missing Contacts grant disables contact lookup, not
the family. The permissions doctor always registers (it is how the owner fixes the
rest). On Linux :func:`chief.app.build_apple_family` returns ``None`` and none of
this exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..inprocess import InProcessServerConfig
from .calendar import AppleCalendarService
from .contacts import ContactsService
from .doctor import AppleDoctorService, CapabilityHealth, probe_all
from .messages import MessagesService
from .notes import NotesService
from .reminders import RemindersService
from .runner import ScriptRunner
from .shortcuts import ShortcutsService
from .system import SystemService


class AppleService(Protocol):
    """What the task engine needs from one Apple app-area service."""

    @property
    def server_name(self) -> str: ...

    @property
    def capability(self) -> str: ...

    def server_config(self) -> InProcessServerConfig: ...


@dataclass
class AppleToolFamily:
    """The family's boot-time builder: probe grants, build the healthy services.

    ``check_health`` is also the hook the web UI health page consumes (#153): it
    returns the doctor's checklist as data (``CapabilityHealth.as_dict`` rows).
    """

    runner: ScriptRunner
    messages_db_path: str
    screenshots_dir: str

    async def check_health(self) -> list[CapabilityHealth]:
        """Probe every capability's TCC grant state (the doctor's data source)."""
        return await probe_all(
            self.runner, messages_db_path=self.messages_db_path
        )

    def build_services(
        self, health: Sequence[CapabilityHealth]
    ) -> tuple[AppleService, ...]:
        """The services to register: one per healthy capability, plus the doctor."""
        healthy = {item.capability for item in health if item.ok}
        services: list[AppleService] = []
        if "reminders" in healthy:
            services.append(RemindersService(runner=self.runner))
        if "notes" in healthy:
            services.append(NotesService(runner=self.runner))
        if "contacts" in healthy:
            services.append(ContactsService(runner=self.runner))
        if "calendar" in healthy:
            services.append(AppleCalendarService(runner=self.runner))
        if "shortcuts" in healthy:
            services.append(ShortcutsService(runner=self.runner))
        if "messages" in healthy:
            services.append(
                MessagesService(runner=self.runner, db_path=self.messages_db_path)
            )
        if "system" in healthy:
            services.append(
                SystemService(
                    runner=self.runner, screenshots_dir=self.screenshots_dir
                )
            )
        services.append(
            AppleDoctorService(
                runner=self.runner, messages_db_path=self.messages_db_path
            )
        )
        return tuple(services)
