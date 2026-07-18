# Uninstalling screening

Screening is one skill file and a registry entry — removing it is quick. Note
that public-facing packages (e.g. build-imessage) rely on screening; don't
remove it while one is installed.

1. Delete the installed skill dir `skills/screening/` (leave the source under
   `packages/screening/` in place — that's how you'd reinstall).
2. Remove the `screening` entry from `data/installed.yaml`.
3. Delete this `UNINSTALL.md` (`packages/screening/UNINSTALL.md`). Its absence
   is the signal the uninstall completed — but keep the rest of the bundled
   package source so screening can be reinstalled later.
4. `restart` to bring the removal live.
