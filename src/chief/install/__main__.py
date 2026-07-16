"""``python -m chief.install`` — the launcher's and install.sh's entrypoint."""

import sys

from chief.install.commands import main

if __name__ == "__main__":
    sys.exit(main())
