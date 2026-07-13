"""Owner-only Apple ecosystem tool family (#155): native Mac integration.

Auto-detected and self-gating: the family registers when chief boots on macOS
(``Settings.apple_configured``) with per-capability permission probes deciding which
app-area services actually wire in; on Linux none of it exists. Subprocess-first —
every tool drives the OS automation layer (AppleScript/JXA via ``osascript``, the
Shortcuts CLI, the sqlite3 CLI for the Messages store) through the
:class:`~chief.tools.apple.runner.ScriptRunner` seam; no PyObjC dependency.

One service per app area (the :mod:`chief.tools.guest` / :mod:`chief.tools.web`
in-process pattern), plus the permissions doctor
(:mod:`chief.tools.apple.doctor`) that maps macOS TCC grants to a per-capability
health checklist with exact System Settings walk-throughs.
"""

from .family import AppleService, AppleToolFamily
from .runner import ScriptRunner

__all__ = ["AppleService", "AppleToolFamily", "ScriptRunner"]
