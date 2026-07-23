# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security tab → Report a
vulnerability** on this repository. Please don't open a public issue for
anything exploitable.

chief is alpha software; only the `main` branch is supported. Reports are
triaged on a best-effort basis.

## Scope worth knowing before you report

chief is a single-owner agent that runs as the owner's user, executes shell
commands after an approval gate, and can edit its own source. "The agent can
run commands the owner approved" is the design, not a vulnerability. The
interesting reports are the ones that break the stated model, e.g.:

- a non-owner sender reaching the agent loop or minting owner trust
- bypassing the tool gate's never/approve lists or the approval card flow
- the web UI serving anything without the owner password
- prompt-injection paths that defeat the screening package's gate

The internal security model is documented in
[docs/SECURITY.md](../docs/SECURITY.md).
