"""Installer + lifecycle plumbing (#154): the code behind the one-line install.

The shell entrypoints (``bootstrap.sh`` → ``install.sh`` → the ``chief`` launcher)
stay deliberately thin; everything with logic worth testing lives here and is
invoked as ``python -m chief.install <command>``:

- :mod:`.wizard` — the interactive first-run wizard (owner password → the web UI
  credential; model auth via Copilot login walk or OpenRouter key paste).
- :mod:`.service` — the autostart service (launchd agent on macOS, systemd user
  unit on Linux) rendered and managed through one :class:`~.service.ServiceManager`.
- :mod:`.lifecycle` — the CLI surface: ``wizard``, ``migrate``, service verbs
  (``start``/``stop``/``status``), ``update``, ``uninstall``, ``await-health``,
  ``open-browser``.
"""
