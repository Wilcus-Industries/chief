"""Sandbox-side code: the stdlib shell server that runs in the secret-free container.

Kept import-light on purpose. :mod:`chief.sandbox.shell_server` is pure stdlib so the
sandbox image can run it by copying the single file — it never imports the rest of the
``chief`` package (telegram/sqlalchemy/the SDK), keeping the sandbox's attack surface
minimal. It lives under ``chief`` only so it is type-checked and unit-tested in the main
venv; the container installs no chief dependencies.
"""
