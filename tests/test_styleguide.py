"""CI enforcement of the STYLEGUIDE file-length cap (part of the done-check)."""

from pathlib import Path

HARD_CAP = 200
ESCAPE_HATCH = "styleguide: file-length"
SRC = Path(__file__).parent.parent / "src"


def test_production_files_stay_under_the_hard_cap() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        lines = path.read_text().splitlines()
        justified = any(ESCAPE_HATCH in line for line in lines[:5])
        if len(lines) >= HARD_CAP and not justified:
            offenders.append(f"{path.relative_to(SRC.parent)}: {len(lines)} lines")
    assert not offenders, (
        "files at or over the 200-line hard cap without a justification comment:\n"
        + "\n".join(offenders)
    )
