"""Drive query-string escaping for the Google Drive MCP server.

Pure stdlib so it is importable by a unit test outside the image — ``server.py`` can't be
imported under the suite (module-load Google auth + heavy deps absent from chief's venv),
yet this escaper is security-critical (it prevents query-literal injection), so it must be
testable. ``server.py`` imports ``_escape_drive_query`` from here.
"""


def _escape_drive_query(value: str) -> str:
    """Escape a value for safe interpolation into a Drive ``q=`` string literal.

    Drive query string literals are single-quoted; an unescaped quote (e.g. a name like
    ``it's.pdf``) would break out of the literal and alter the query. Backslash first so
    we don't double-escape the quote escapes we add.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")
