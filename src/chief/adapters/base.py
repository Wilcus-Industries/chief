"""The adapter interface. Channels are dumb pipes: deliver inbound Messages,
expose send. Nothing channel-specific lives outside the adapter itself."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    """One inbound message from a channel."""

    channel: str
    sender: str
    thread_key: str
    text: str
    attachments: tuple[str, ...] = ()


class Adapter(ABC):
    """A channel endpoint. Subclasses deliver inbound Messages to the
    dispatcher callback they were constructed with."""

    name: str

    @abstractmethod
    async def start(self) -> None:
        """Begin accepting inbound traffic."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop accepting traffic and release resources."""

    @abstractmethod
    async def send(self, thread_key: str, text: str) -> None:
        """Deliver the final reply for a thread."""

    async def send_delta(self, thread_key: str, text: str) -> None:  # noqa: B027
        """Stream a partial reply chunk; channels without streaming ignore it.

        Deliberately a no-op default, not abstract: most channels don't stream.
        """
