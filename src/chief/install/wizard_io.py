"""The wizard's terminal surface and shared constants.

Its own module so :mod:`.wizard` and :mod:`.wizard_steps` can both depend on
it without importing each other.
"""

import getpass
from collections.abc import Callable
from dataclasses import dataclass

MIN_PASSWORD_LENGTH = 8
KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"

#: Returns None when the key works, else a short error message.
KeyValidator = Callable[[str], str | None]


@dataclass
class WizardIO:
    """The wizard's whole terminal surface, injectable for tests."""

    prompt: Callable[[str], str] = input
    prompt_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print
