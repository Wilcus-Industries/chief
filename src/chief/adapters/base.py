"""The adapter interface. Channels are dumb pipes: deliver inbound Messages,
expose send. Nothing channel-specific lives outside the adapter itself."""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from chief.agent.restart_gate import TurnTicket


@dataclass(frozen=True)
class Message:
    """One inbound message from a channel.

    ``at`` is when the message arrived (epoch seconds): the time its transcript
    stamp names, however long its turn then waits behind others. It defaults to
    when the message is built; a channel that knows better passes it (iMessage,
    for a row sent while chief was down). A replay carries the original; it is
    not part of a message's identity."""

    channel: str
    sender: str
    thread_key: str
    text: str
    attachments: tuple[str, ...] = ()
    at: float = field(default_factory=time.time, compare=False)


# ``Dispatcher.try_inject``: offer a message to its thread's running turn, with
# the caller's own queue path to hand it back through if the turn ends first —
# along with its in-flight record, which the queue path must keep alive.
Requeue = Callable[[Message, TurnTicket], None]
Inject = Callable[[Message, Requeue], bool]


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
