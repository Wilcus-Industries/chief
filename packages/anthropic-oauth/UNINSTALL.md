# Uninstalling anthropic-oauth

Reverts the provider from the local subscription proxy back to OpenRouter. No
core code is touched — this only unwires config and the skill.

1. Point the provider back at OpenRouter: set
   `provider_base_url: https://openrouter.ai/api/v1` in `config.yaml`
   (`edit_file` the key, or run
   `python -m chief.config_apply provider_base_url=https://openrouter.ai/api/v1`
   via Bash). The default is OpenRouter, so clearing the key entirely also works.
2. Restore an OpenRouter model. The bare model name the proxy used will not
   resolve on OpenRouter — ask the owner which OpenRouter model to run and set
   `models.default` to it (e.g. `python -m chief.config_apply
   models.default=qwen/qwen3-coder`).
3. Restore the OpenRouter bearer. If `secrets/openrouter_api_key` was overwritten
   with the proxy's `CCI_API_KEY` at install, write the owner's real OpenRouter
   key back into it. Without a valid key every turn will fail.
4. Delete the installed skill dir `skills/anthropic-oauth/`.
5. Remove the `anthropic-oauth` entry from `data/installed.yaml`.
6. Delete this `UNINSTALL.md` (`packages/anthropic-oauth/UNINSTALL.md`) — its
   absence signals the uninstall completed.
7. `restart` to bring the change live. The owner can then stop the proxy process
   and, if desired, `claude logout` on the host.
