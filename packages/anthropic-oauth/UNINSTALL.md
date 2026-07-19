# Uninstalling anthropic-oauth

Removes the proxy backend and its aliases so every thread routes back to the
default backend (OpenRouter). No core code is touched — this only unwires
config, the secret, and the skill.

1. Drop the proxy backend and its aliases from `config.yaml`. `config_apply`
   only merges, so remove them with your file tools: delete the
   `provider_backends.proxy` entry and every `provider_aliases` entry whose
   `backend` is `proxy` (e.g. `opus`, `sonnet`). If those were the only
   backends/aliases, remove the now-empty `provider_backends:` and
   `provider_aliases:` keys entirely — an empty routing table restores the
   plain single-provider (OpenRouter) behavior.
2. Move any thread that was pinned to a proxied alias back onto a default
   model: `/model qwen/qwen3-coder` (or whatever the owner runs) in that
   thread, so it does not error on a now-unknown alias.
3. Delete the package secret `secrets/proxy_api_key`. The OpenRouter key in
   `secrets/openrouter_api_key` was never touched, so the default backend keeps
   working.
4. Delete the installed skill dir `skills/anthropic-oauth/`.
5. Deregister: `uv run python -m chief.registry_apply anthropic-oauth --remove`.
6. Delete this `UNINSTALL.md` (`packages/anthropic-oauth/UNINSTALL.md`) — its
   absence signals the uninstall completed.
7. `restart` to bring the change live. The owner can then stop the proxy
   process and, if desired, `claude logout` on the host.
