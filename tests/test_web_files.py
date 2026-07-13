"""Web file access tests (#153): workspace + screenshots only, fenced server-side.

Real HTTP against the real app (ASGITransport — no streaming needed here); the file
areas are real directories under tmp_path. Every fence (area allowlist, traversal,
absolute paths, upload sanitization) is asserted through the HTTP surface.
"""

from pathlib import Path

import httpx
import pytest

from chief.web.app import WebDeps, build_web_app
from chief.web.auth import WebAuth
from chief.web.files import FileAreas

PASSWORD = "filesuitepassword"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def screenshots(tmp_path: Path) -> Path:
    d = tmp_path / "screenshots"
    d.mkdir()
    return d


@pytest.fixture
async def client(
    tmp_path: Path, workspace: Path, screenshots: Path
) -> httpx.AsyncClient:
    auth = WebAuth(tmp_path / "secrets")
    auth.set_password(PASSWORD)
    app = build_web_app(
        WebDeps(
            auth=auth,
            files=FileAreas(workspace=workspace, screenshots=screenshots),
        )
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    )
    resp = await http.post("/login", data={"password": PASSWORD})
    assert resp.status_code == 303
    return http


async def test_upload_then_list_then_download_round_trip(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    resp = await client.post(
        "/files/upload",
        files={"file": ("notes.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 303
    assert (workspace / "notes.pdf").read_bytes() == b"%PDF-1.4 fake"

    page = await client.get("/files?area=workspace")
    assert page.status_code == 200
    assert "notes.pdf" in page.text

    download = await client.get("/files/download?area=workspace&path=notes.pdf")
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 fake"
    assert "attachment" in download.headers["content-disposition"]


async def test_upload_filename_is_sanitized_to_its_basename(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    resp = await client.post(
        "/files/upload",
        files={"file": ("../../escape.txt", b"nope", "text/plain")},
    )
    assert resp.status_code == 303
    assert (workspace / "escape.txt").read_bytes() == b"nope"
    assert not (tmp_path / "escape.txt").exists()


async def test_upload_collision_keeps_both_files(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    for body in (b"one", b"two"):
        resp = await client.post(
            "/files/upload", files={"file": ("dup.txt", body, "text/plain")}
        )
        assert resp.status_code == 303
    names = {p.name for p in workspace.iterdir()}
    assert names == {"dup.txt", "dup-1.txt"}
    assert (workspace / "dup.txt").read_bytes() == b"one"
    assert (workspace / "dup-1.txt").read_bytes() == b"two"


async def test_download_traversal_and_absolute_paths_are_refused(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    (tmp_path / "secret.txt").write_text("keep out")
    resp = await client.get("/files/download?area=workspace&path=../secret.txt")
    assert resp.status_code == 404
    resp = await client.get(
        f"/files/download?area=workspace&path={tmp_path / 'secret.txt'}"
    )
    assert resp.status_code == 404


async def test_screenshots_area_is_readable_but_not_writable(
    client: httpx.AsyncClient, screenshots: Path
) -> None:
    (screenshots / "page.png").write_bytes(b"\x89PNG fake")
    page = await client.get("/files?area=screenshots")
    assert "page.png" in page.text

    download = await client.get("/files/download?area=screenshots&path=page.png")
    assert download.status_code == 200
    assert download.content == b"\x89PNG fake"

    resp = await client.post(
        "/files/upload",
        data={"area": "screenshots"},
        files={"file": ("sneak.png", b"x", "image/png")},
    )
    assert resp.status_code == 400
    assert not (screenshots / "sneak.png").exists()


async def test_unknown_area_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/files/download?area=etc&path=passwd")
    assert resp.status_code == 404
    resp = await client.get("/files?area=etc")
    assert resp.status_code == 404


async def test_nested_workspace_files_are_listed_and_downloadable(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    nested = workspace / "out"
    nested.mkdir()
    (nested / "result.csv").write_text("a,b\n1,2\n")
    page = await client.get("/files?area=workspace")
    assert "out/result.csv" in page.text
    download = await client.get(
        "/files/download?area=workspace&path=out/result.csv"
    )
    assert download.status_code == 200
    assert download.text == "a,b\n1,2\n"


async def test_files_require_a_session(tmp_path: Path, workspace: Path) -> None:
    auth = WebAuth(tmp_path / "other-secrets")
    auth.set_password(PASSWORD)
    app = build_web_app(
        WebDeps(auth=auth, files=FileAreas(workspace=workspace, screenshots=None))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web"
    ) as anon:
        resp = await anon.get("/files")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
