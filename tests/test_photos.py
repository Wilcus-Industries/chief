"""Owner-only photos: stashed from chat.db, carried by path, sent as images."""

import asyncio
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from chief.adapters.base import Message
from chief.adapters.imessage import IMessageAdapter
from chief.adapters.imessage_attachments import NOT_SHOWN, PHOTO, ImageStash
from chief.adapters.imessage_history import HistoryStore, IMessageHistory
from chief.adapters.imessage_map import map_row
from chief.agent.compaction import estimate_tokens
from chief.agent.restart_gate import TurnTicket
from chief.agent.session import Session
from chief.agent.timestamps import user_message
from chief.bus import EventBus
from chief.persistence.store import MessageStore
from chief.provider.anthropic_wire import to_anthropic_request
from chief.provider.base import for_model
from chief.provider.images import (
    IMAGE_TOKENS,
    IMAGES_KEY,
    MAX_LOADED,
    MISSING,
    STUB,
)
from chief.selfedit.inflight import InflightLog
from chief.tools import ToolRegistry

from .fakes import FakeProvider, text_turn
from .test_imessage import FakeStore
from .test_monitor_history import GC, HENRY, OWNER, FakeHistory, gc_service, watch

MARK = "￼"
JPEG = b"\xff\xd8\xff\xe0fakejpeg"


class PhotoStore(FakeStore):
    """FakeStore plus the attachment tables a photo row joins to."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT,"
                " mime_type TEXT, transfer_name TEXT, total_bytes INTEGER);"
                "CREATE TABLE message_attachment_join (message_id INTEGER,"
                " attachment_id INTEGER);")

    def add_photo(self, sender: str, caption: str, image: Path, **kw: Any) -> int:
        self.add_message(sender, f"{MARK}{caption}", **kw)
        with sqlite3.connect(self.path) as conn:
            rowid = int(conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0])
            att = conn.execute(
                "INSERT INTO attachment (filename, mime_type) VALUES (?, ?)",
                (str(image), "image/heic")).lastrowid
            conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
                         (rowid, att))
        return rowid


class CopyConvert:
    """Stands in for sips: copies the source, counting conversions."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, src: Path, dst: Path) -> bool:
        self.calls.append(src)
        shutil.copy(src, dst)
        return True


def picture(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_1.HEIC"
    path.write_bytes(JPEG)
    return path


# --- stashing --------------------------------------------------------------


def test_labels_open_nothing_and_only_the_owners_photo_is_stashed(
    tmp_path: Path,
) -> None:
    store, convert = PhotoStore(tmp_path / "chat.db"), CopyConvert()
    stash = ImageStash(tmp_path / "stash", convert, wait=0)
    mine = store.add_photo(OWNER, "look", picture(tmp_path))
    theirs = store.add_photo(HENRY, "look", picture(tmp_path))
    labelled = stash.label(store.path, mine, f"{MARK}look", owner=True)
    assert labelled.text == f"{PHOTO} look" and labelled.owner_photos
    other = stash.label(store.path, theirs, f"{MARK}look", owner=False)
    assert other.text == f"{NOT_SHOWN} look" and not other.owner_photos
    assert convert.calls == [], "labelling converts nothing"
    paths = stash.stash(store.path, mine)
    assert len(paths) == 1 and Path(paths[0]).read_bytes() == JPEG


def test_a_stashed_photo_is_converted_once(tmp_path: Path) -> None:
    store, convert = PhotoStore(tmp_path / "chat.db"), CopyConvert()
    stash = ImageStash(tmp_path / "stash", convert, wait=0)
    rowid = store.add_photo(OWNER, "", picture(tmp_path))
    assert stash.stash(store.path, rowid) == stash.stash(store.path, rowid)
    assert len(convert.calls) == 1


def test_twin_rows_share_a_fingerprint_and_two_photos_do_not(
    tmp_path: Path,
) -> None:
    store = PhotoStore(tmp_path / "chat.db")
    stash = ImageStash(tmp_path / "stash", CopyConvert(), wait=0)
    one = store.add_photo(OWNER, "", picture(tmp_path))
    other = tmp_path / "IMG_2.HEIC"
    other.write_bytes(JPEG + b"different")
    two = store.add_photo(OWNER, "", other)
    first = stash.label(store.path, one, MARK, True)
    second = stash.label(store.path, two, MARK, True)
    assert first.text == second.text == PHOTO
    assert first.fingerprint != second.fingerprint, "two photos are two messages"


def test_tilde_paths_resolve_against_the_stores_home(tmp_path: Path) -> None:
    home = tmp_path / "chiefhome"
    (home / "Library/Messages").mkdir(parents=True)
    (home / "pics").mkdir()
    (home / "pics/a.jpg").write_bytes(JPEG)
    store, convert = PhotoStore(home / "Library/Messages/chat.db"), CopyConvert()
    rowid = store.add_photo(OWNER, "", Path("~/pics/a.jpg"))
    ImageStash(tmp_path / "stash", convert, wait=0).stash(store.path, rowid)
    assert convert.calls == [home / "pics/a.jpg"]


def test_a_failed_conversion_carries_nothing(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path / "chat.db")
    rowid = store.add_photo(OWNER, "hi", picture(tmp_path))
    stash = ImageStash(tmp_path / "s", lambda s, d: False, wait=0)
    assert stash.stash(store.path, rowid) == ()


def test_text_without_an_attachment_passes_through(tmp_path: Path) -> None:
    stash = ImageStash(tmp_path / "s", CopyConvert())
    assert stash.label(tmp_path / "none.db", 1, "plain", True).text == "plain"


# --- the poller ------------------------------------------------------------


async def test_owner_photo_reaches_the_turn_and_a_strangers_does_not(
    tmp_path: Path,
) -> None:
    store = PhotoStore(tmp_path / "chat.db")
    store.add_photo(OWNER, "what is this", picture(tmp_path), chat=OWNER)
    store.add_photo(HENRY, "", picture(tmp_path))
    delivered: list[Message] = []

    async def on_message(message: Message, ticket: TurnTicket) -> None:
        delivered.append(message)

    async def run_jxa(script: str, argv: tuple[str, ...]) -> str:
        return "sent"

    adapter = IMessageAdapter(
        on_message, db_path=store.path, cursor_path=tmp_path / "cursor",
        owner_handles=(OWNER,), run_jxa=run_jxa, dedicated=True,
        stash=ImageStash(tmp_path / "stash", CopyConvert(), wait=0))
    await adapter.poll_once()
    await adapter.drain()
    owner, stranger = delivered
    assert owner.sender == "owner" and owner.text == f"{PHOTO} what is this"
    assert len(owner.attachments) == 1
    assert stranger.text == NOT_SHOWN and stranger.attachments == ()


# --- the transcript --------------------------------------------------------


def test_only_the_model_call_loads_images(tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(JPEG)
    stored = [user_message("look", images=(str(path), str(tmp_path / "gone.jpg")))]
    assert stored[0][IMAGES_KEY] == [str(path), str(tmp_path / "gone.jpg")]
    sent = for_model(stored, images=True)[0]["content"]
    assert sent[0]["type"] == "text" and sent[0]["text"].endswith("look")
    assert sent[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert sent[2] == {"type": "text", "text": MISSING}
    stub = for_model(stored)[0]["content"]
    assert isinstance(stub, str) and stub.endswith(f"look {STUB} {STUB}")


def test_a_photo_costs_a_flat_estimate_not_its_base64(tmp_path: Path) -> None:
    path = tmp_path / "big.jpg"
    path.write_bytes(b"\xff" * 2_000_000)
    plain = [user_message("look")]
    with_photo = [user_message("look", images=(str(path),))]
    extra = estimate_tokens(with_photo) - estimate_tokens(plain)
    assert IMAGE_TOKENS <= extra < IMAGE_TOKENS + 10


def test_anthropic_wire_sends_image_blocks(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(b"\x89PNG")
    messages = for_model([user_message("see", images=(str(path),))], images=True)
    body = to_anthropic_request("m", messages, [], max_tokens=10)
    blocks = body["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"


async def test_session_persists_paths_and_sends_the_image(
    store: MessageStore, tmp_path: Path
) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(JPEG)
    await store.ensure_session("t", "imessage")
    provider = FakeProvider([text_turn("a cat")])
    session = Session(
        thread_key="t", provider=provider, tools=ToolRegistry(), store=store,
        model="m", system_prompt="s", history=[],
        turn_semaphore=asyncio.Semaphore(1))
    await session.run_turn("what is it", lambda _: asyncio.sleep(0),
                           images=(str(path),))
    persisted = await store.load("t")
    assert persisted[0][IMAGES_KEY] == [str(path)]
    assert "base64" not in json.dumps(persisted), "the row keeps the path only"
    sent = provider.calls[0][-1]["content"]
    assert any(part.get("type") == "image_url" for part in sent)


# --- wakes -----------------------------------------------------------------


async def test_wake_attaches_the_owners_photos_only(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    bus, history = EventBus(), FakeHistory()
    service, wake = gc_service(engine, bus, history, tmp_path)
    await watch(service)
    history.say(OWNER, f"{PHOTO} chief rate my fit")
    history.lines[-1] = replace(history.lines[-1], owner_photos=True)
    await bus.publish(history.say(HENRY, f"{NOT_SHOWN} chief look"))
    sent = wake.messages[0]
    assert sent.attachments == ("/stash/1.jpg",)
    assert 'Will (owner): "[photo] chief rate my fit"' in sent.text
    assert '"[photo, not shown] chief look"' in sent.text


def test_history_stashes_owner_photos_in_a_shared_chat(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path / "chat.db")
    store.add_photo(OWNER, "fit check", picture(tmp_path), group=True, chat=GC)
    store.add_photo(HENRY, "", picture(tmp_path), group=True, chat=GC)
    convert = CopyConvert()
    history = IMessageHistory([HistoryStore(store.path, "chief")], (OWNER,),
                              stash=ImageStash(tmp_path / "stash", convert, wait=0))
    source, (owner, henry) = history.recent(GC, 10)
    assert owner.text == f"{PHOTO} fit check" and owner.owner_photos
    assert henry.text == NOT_SHOWN and not henry.owner_photos
    assert convert.calls == [], "reading history stashes nothing"
    assert len(history.photos(source, owner)) == 1
    assert history.photos(source, henry) == ()


def test_only_the_newest_photos_load(tmp_path: Path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(JPEG)
    messages = [user_message(f"m{i}", images=(str(path),))
                for i in range(MAX_LOADED + 2)]
    sent = for_model(messages, images=True)
    assert isinstance(sent[0]["content"], str) and isinstance(sent[1]["content"], str)
    assert all(isinstance(m["content"], list) for m in sent[2:])


def test_a_replay_keeps_its_photos(tmp_path: Path) -> None:
    log = InflightLog(tmp_path)
    log.track("t", "imessage", "owner", "[photo]", attachments=("/s/a.jpg",))
    (turn,) = InflightLog(tmp_path).take_interrupted()
    assert turn.attachments == ("/s/a.jpg",)


def test_a_handle_spelled_like_a_role_is_never_that_role() -> None:
    for sender in ("owner", "system"):
        one = map_row("imessage", sender, "hi", 0, None, 0, 0, frozenset(), True)
        group = map_row("imessage", sender, "hi", 0, "chat1", 0, 0, frozenset(), True)
        assert one is not None and one.sender == f"handle:{sender}"
        assert group is not None and group.sender == f"handle:{sender}"
