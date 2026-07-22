"""Root conftest — session-wide setup."""

import resource


def pytest_configure(config: object) -> None:
    """Raise the open-file-descriptor limit so the full suite doesn't hit EMFILE."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 4096
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))
