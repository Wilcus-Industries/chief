"""Installer + lifecycle plumbing: the code behind the one-line install.

The shell entrypoints (``bootstrap.sh`` → ``install.sh`` → the ``chief``
launcher) stay deliberately thin; everything with logic worth testing lives
here and is invoked as ``python -m chief.install <command>``:

- :mod:`.wizard` — first-run wizard: owner password (web UI credential),
  OpenRouter API key, and the monthly budget cap.
- :mod:`.service` — autostart service (launchd agent on macOS, systemd user
  unit on Linux) behind one :class:`~.service.ServiceManager`.
- :mod:`.lifecycle` — the CLI: ``wizard``, service verbs, ``update``,
  ``uninstall``, ``await-health``, ``open-browser``. No migrations — the
  schema is created at boot.
"""
