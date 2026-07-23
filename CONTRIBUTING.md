# Contributing

chief is a personal project in active alpha — things move around weekly.
Issues are very welcome: bugs, sharp edges in the install, design questions.
PRs are accepted, but open an issue first for anything non-trivial so you
don't build against code that's about to change.

## Dev setup

```sh
git clone https://github.com/Wilcus-Industries/chief
cd chief
uv sync --group dev
```

## The done-check

Every change must pass all three before it's done:

```sh
uv run pytest
uv run ruff check .
uv run mypy .
```

CI runs the same three plus `shellcheck` on the installer scripts.

## Conventions

- [STYLEGUIDE.md](./STYLEGUIDE.md) is the rulebook — read it first.
- Production files are capped at 200 lines (CI-enforced); write tests first.
- Packages generally belong in
  [chief-packages](https://github.com/Wilcus-Industries/chief-packages), not
  this repo — see [CLAUDE.md](./CLAUDE.md) for the boundary.

## Security

Don't open public issues for vulnerabilities — see the
[security policy](./.github/SECURITY.md).
