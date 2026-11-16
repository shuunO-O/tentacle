"""Synthetic outgoing file checks; no credentials, private history or live QQ."""

import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import attachments.outgoing as files
import plugins.bundled.qqbot.adapter as qq_adapter_mod
from plugins.bundled.qqbot.adapter import QQAdapter
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


# The same cases run against Python (QQ) and browser JavaScript (Web).
MARKER_CASES = [
    ("", []),
    ("ordinary text", [("text", "ordinary text")]),
    ("before [file:tmp/报告 & a_b*.pdf] after", [
        ("text", "before "), ("file", "tmp/报告 & a_b*.pdf"), ("text", " after")]),
    ("[file: /tmp/a ][file:/tmp/b]", [("file", "/tmp/a"), ("file", "/tmp/b")]),
    ("[file:][file:   ][file:unfinished", [("text", "[file:][file:   ][file:unfinished")]),
    ("[file:a\nb]", [("text", "[file:a\nb]")]),
    ("`[file:a]` and ``[file:b]``", [("text", "`[file:a]` and ``[file:b]``")]),
    ("\\[file:a] [file:b]", [("text", "\\[file:a] "), ("file", "b")]),
    ("```text\n[file:a]\n```\n[file:b]", [("text", "```text\n[file:a]\n```\n"), ("file", "b")]),
    ("~~~\r\n[file:a]\r\n~~~\r\n[file:b]", [("text", "~~~\r\n[file:a]\r\n~~~\r\n"), ("file", "b")]),
    ("````\n```\n[file:a]\n````\n[file:b]", [("text", "````\n```\n[file:a]\n````\n"), ("file", "b")]),
    ("```\n[file:a]", [("text", "```\n[file:a]")]),
    ("[file:tmp/a\"<img onerror=x>.txt]", [("file", 'tmp/a"<img onerror=x>.txt')]),
]


@pytest.mark.parametrize("text,expected", MARKER_CASES)
def test_marker_grammar(text, expected):
    assert files.split_file_markers(text) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="JavaScript checks require Node")
def test_browser_marker_grammar_matches_python():
    module = Path(__file__).resolve().parents[1] / "frontend/src/features/chat/fileMarkers.ts"
    script = (
        "import { pathToFileURL } from 'node:url';"
        "const { splitFileMarkers } = await import(pathToFileURL(process.argv[1]).href);"
        "let s = '';"
        "process.stdin.on('data', d => s += d);"
        "process.stdin.on('end', () => {"
        "  const out = JSON.parse(s).map((t) => splitFileMarkers(t).map((p) => p.kind === 'file' ? ['file', p.path] : ['text', p.text]));"
        "  process.stdout.write(JSON.stringify(out));"
        "});"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(module)],
        input=json.dumps([t for t, _ in MARKER_CASES]),
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert json.loads(result.stdout) == [[list(part) for part in expected] for _, expected in MARKER_CASES]


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    path = tmp_path / "报告 & a_b*.txt"
    path.write_bytes(b"synthetic attachment\x00\xff")
    return path


def test_relative_absolute_and_allowed_symlink(sample):
    link = sample.parent / "alias.txt"
    link.symlink_to(sample)
    for value in (sample.name, str(sample), str(link)):
        path, stream, metadata = files.open_outgoing_file(value)
        with stream:
            assert path == sample
            assert stream.read() == sample.read_bytes()
            assert metadata.st_size == len(sample.read_bytes())


@pytest.mark.parametrize("name", [
    ".env", ".env.production", ".env.bak", ".ssh/private", ".git/config", "credentials/other.json",
    "gcloud/application_default_credentials.json", "google-sa.json", "x-service-account.json",
    "github-token", "webui_settings.json", "webui_settings.json.bak", "device_enroll_pending.json",
    "device_auth_freeze.json", "key.pem", "private.key", ".config/gh/hosts.yml",
])
def test_sensitive_paths_and_aliases_blocked(sample, name):
    secret = sample.parent / name
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("synthetic secret")
    alias = sample.parent / "innocent.txt"
    alias.symlink_to(secret)
    for path in (secret, alias):
        with pytest.raises(PermissionError):
            files.open_outgoing_file(str(path))


def test_custom_adc_path_and_custom_settings_are_protected(sample, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sample))
    with pytest.raises(PermissionError):
        files.open_outgoing_file(str(sample))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    with pytest.raises(PermissionError):
        files.open_outgoing_file(str(sample), protected_paths=(sample,))


def test_special_files_directories_and_hardlinks_rejected(sample):
    fifo = sample.parent / "pipe"
    os.mkfifo(fifo)
    for value in (str(sample.parent), str(fifo), "/proc/self/environ", "/dev/null"):
        with pytest.raises(PermissionError):
            files.open_outgoing_file(value)
    hardlink = sample.parent / "hardlink"
    hardlink.hardlink_to(sample)
    with pytest.raises(PermissionError):
        files.open_outgoing_file(str(hardlink))


def test_component_replacement_cannot_redirect_open(sample, monkeypatch):
    folder = sample.parent / "folder"
    folder.mkdir()
    (folder / "document").write_text("public")
    secret = sample.parent / "secrets"
    secret.mkdir()
    (secret / "document").write_text("synthetic private")
    original = files.os.open

    def swap(path, flags, **kwargs):
        if path == "folder":
            folder.rename(sample.parent / "previous")
            folder.symlink_to(secret, target_is_directory=True)
        return original(path, flags, **kwargs)

    monkeypatch.setattr(files.os, "open", swap)
    with pytest.raises(OSError):
        files.open_outgoing_file("folder/document")


@pytest.fixture
async def web_runtime(tmp_path, sample):
    settings = WebSettings(str(tmp_path / "webui_settings.json"))
    app = create_app(None, None, None, None, None, SimpleNamespace(db_path=tmp_path / "unused.db"), settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        yield settings, client


async def test_download_auth_filename_errors_and_binary_content(web_runtime, sample):
    settings, client = web_runtime
    settings.set_password("synthetic-password")
    settings.set_device_auth(True)
    params = {"path": str(sample)}
    assert (await client.get("/api/files/download", params=params)).status_code == 401
    client.headers["Authorization"] = "Bearer " + settings.issue_token()
    assert (await client.get("/api/files/download", params=params)).status_code == 401
    client.cookies.set(DEVICE_COOKIE, settings.create_device("synthetic-device"))
    response = await client.get("/api/files/download", params=params)
    assert response.status_code == 200
    assert response.content == sample.read_bytes()
    assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''%")
    assert response.headers["content-type"] == "application/octet-stream"
    assert "no-store" in {part.strip() for part in response.headers["cache-control"].split(",")}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (await client.get("/api/files/download", params={"path": ".env"})).status_code == 403
    assert (await client.get("/api/files/download", params={"path": "deleted.txt"})).status_code == 404
    assert (await client.get("/api/files/download", params={"path": str(sample.parent)})).status_code == 403
    client.headers.clear()
    assert (await client.get("/api/files/download", params=params)).status_code == 401


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(QQAdapter, "_load_latest_chat", lambda self: None)
    result = QQAdapter(None, default_to="synthetic-user")
    result._send_interval = 0
    return result


async def test_qq_upload_then_send_and_original_order(adapter, sample):
    calls = []

    async def api(method, path, body):
        calls.append((path, body))
        return {"file_info": "synthetic-file-info"} if path.endswith("/files") else {"id": "confirmed"}

    adapter._api_request = AsyncMock(side_effect=api)
    result = await adapter._deliver(f"before [file:{sample}] after")
    assert [body.get("content", body.get("file_type", body.get("msg_type"))) for _, body in calls] == ["before", 4, 7, "after"]
    upload = calls[1][1]
    assert base64.b64decode(upload["file_data"]) == sample.read_bytes()
    assert upload["file_name"] == sample.name
    assert upload["srv_send_msg"] is False
    assert calls[2][1]["media"] == {"file_info": "synthetic-file-info"}
    assert result == {"sent_chunks": 3, "total_chunks": 3, "sent_files": 1}


@pytest.mark.parametrize("failure", ["upload", "missing_info", "send"])
async def test_qq_upload_or_send_failure_never_reports_success_or_retries(adapter, sample, failure):
    responses = {
        "upload": [TimeoutError("synthetic timeout")],
        "missing_info": [{}],
        "send": [{"file_info": "synthetic-info"}, TimeoutError("synthetic timeout")],
    }
    adapter._api_request = AsyncMock(side_effect=responses[failure])
    with pytest.raises(RuntimeError, match="已确认发送 0/1"):
        await adapter._deliver(f"[file:{sample}]")
    assert adapter._api_request.await_count == (2 if failure == "send" else 1)


async def test_qq_send_waits_for_final_confirmation(adapter, sample):
    entered, release = asyncio.Event(), asyncio.Event()

    async def api(method, path, body):
        if path.endswith("/files"):
            return {"file_info": "synthetic-info"}
        entered.set()
        await release.wait()
        return {"id": "confirmed"}

    adapter._api_request = api
    task = asyncio.create_task(adapter._deliver(f"[file:{sample}]"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not task.done()
    finally:
        release.set()
        await task


async def test_qq_partial_delivery_stops_at_failure(adapter, sample):
    adapter._send_c2c_text = AsyncMock()
    adapter._send_c2c_file = AsyncMock(side_effect=TimeoutError("synthetic timeout"))
    with pytest.raises(RuntimeError, match="已确认发送 1/3"):
        await adapter._deliver(f"before [file:{sample}] after")
    adapter._send_c2c_text.assert_awaited_once_with("synthetic-user", "before")
    assert adapter._send_c2c_file.await_count == 1


async def test_qq_protected_missing_oversized_and_empty_files_do_not_upload(adapter, sample, monkeypatch):
    adapter._api_request = AsyncMock()
    for value in (".env", "deleted", str(sample.parent)):
        with pytest.raises(RuntimeError):
            await adapter._deliver(f"[file:{value}]")
    monkeypatch.setattr(qq_adapter_mod, "QQ_MAX_FILE_BYTES", 2)
    with pytest.raises(RuntimeError, match="20 MiB"):
        await adapter._deliver(f"[file:{sample}]")
    sample.write_bytes(b"")
    with pytest.raises(RuntimeError, match="空文件"):
        await adapter._deliver(f"[file:{sample}]")
    adapter._api_request.assert_not_awaited()


async def test_qq_code_examples_stay_text_even_with_small_chunks(adapter):
    adapter._max_chunk = 5
    adapter._send_c2c_text = AsyncMock()
    adapter._send_c2c_file = AsyncMock()
    text = "```\n[file:a]\n```"
    await adapter._deliver(text)
    assert "".join(c.args[1] for c in adapter._send_c2c_text.await_args_list) == text
    adapter._send_c2c_file.assert_not_awaited()
