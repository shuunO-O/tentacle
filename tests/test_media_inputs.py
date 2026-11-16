"""Synthetic media ingestion checks: no credentials, model calls or live downloads."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import agent.session as sessions
import attachments.manager as files
import attachments.media as media
from agent.session import AgentSession
from dialects import get_dialect
from agent.protocol import Context, EventType, LoopResult, ModelConfig


@pytest.mark.parametrize("text,expected", [
    ("看看 https://b23.tv/abc123。", ["https://b23.tv/abc123"]),
    ("(https://www.bilibili.com/video/BV123?p=2)", ["https://www.bilibili.com/video/BV123?p=2"]),
    ("https://www.bilibili.com.evil.test/video/BV123 https://b23.tv@127.0.0.1/x", []),
    ("https://user@b23.tv/abc https://b23.tv:123/abc", []),
    ("https://b23.tv/abc https://b23.tv/abc", ["https://b23.tv/abc"]),
])
def test_link_detection(text, expected):
    assert media.bilibili_links(text) == expected


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/video/BV123", "https://b23.tv.evil.test/x",
    "https://bilibili.com/video/BV123?p=0", "https://bilibili.com/video/BV123?p=bad",
    "https://www.bilibili.com/bangumi/play/ss1", "file:///etc/passwd",
])
async def test_url_rejected_before_network(url):
    with pytest.raises(ValueError):
        await media.canonical_bilibili_url(url)


async def test_canonical_single_part_strips_tracking():
    assert await media.canonical_bilibili_url("http://m.bilibili.com/video/BV123/?p=3&share=abc") == (
        "https://www.bilibili.com/video/BV123?p=3")


async def test_short_link_external_redirect_rejected(monkeypatch):
    from yarl import URL
    response = SimpleNamespace(status=302, headers={"Location": "http://127.0.0.1/private"}, url=URL("https://b23.tv/abc"))
    cm = Mock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock())
    session = Mock(get=Mock(return_value=cm))
    monkeypatch.setattr(media.aiohttp, "ClientSession", Mock(return_value=Mock(
        __aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())))
    with pytest.raises(ValueError):
        await media.canonical_bilibili_url("https://b23.tv/abc")
    session.get.assert_called_once_with("https://b23.tv/abc", allow_redirects=False)


@pytest.mark.parametrize("fmt,streams,mime", [
    ("mov,mp4,m4a,3gp,3g2,mj2", [{"codec_type": "video"}, {"codec_type": "audio"}], "video/mp4"),
    ("mov,mp4,m4a", [{"codec_type": "audio"}], "audio/m4a"),
    ("mp3", [{"codec_type": "audio"}, {"codec_type": "video", "disposition": {"attached_pic": 1}}], "audio/mp3"),
    ("wav", [{"codec_type": "audio"}], "audio/wav"),
    ("matroska,webm", [{"codec_type": "video"}], "video/webm"),
])
async def test_inspection_uses_actual_container(tmp_path, monkeypatch, fmt, streams, mime):
    path = tmp_path / "wrong-extension.png"
    path.write_bytes(b"synthetic webm media")
    probe = AsyncMock(return_value=json.dumps({"format": {"format_name": fmt, "duration": "10"}, "streams": streams}).encode())
    monkeypatch.setattr(media, "run_program", probe)
    assert await media.inspect_media(path) == mime
    assert "-protocol_whitelist" in probe.call_args.args[0]
    assert "-format_whitelist" in probe.call_args.args[0]


@pytest.mark.parametrize("duration", ["0", "601", "NaN", "inf"])
async def test_invalid_duration(tmp_path, monkeypatch, duration):
    path = tmp_path / "clip"
    path.write_bytes(b"synthetic")
    monkeypatch.setattr(media, "run_program", AsyncMock(return_value=json.dumps({
        "format": {"format_name": "mp3", "duration": duration}, "streams": [{"codec_type": "audio"}],
    }).encode()))
    with pytest.raises(ValueError, match="时长"):
        await media.inspect_media(path)


@pytest.fixture
def fm(tmp_path, monkeypatch):
    monkeypatch.setattr(files.FileManager, "_instance", None)
    manager = files.FileManager(str(tmp_path / "test.db"))
    manager._configured = True
    manager._base_url = "gs://synthetic/media"
    uploader = SimpleNamespace(name="gcs", persistent=True, base_url=manager._base_url,
        upload_path=AsyncMock(return_value=("gs://synthetic/media/clip.mp4", time.time() + 172800)),
        delete=AsyncMock())
    manager._uploader = uploader
    manager._uploaders = {"gcs": uploader}
    monkeypatch.setattr(media, "inspect_media", AsyncMock(return_value="video/mp4"))
    return manager


async def test_streamed_upload_dedup_history_and_persistent_cleanup(fm, tmp_path, monkeypatch):
    path = tmp_path / "video"
    path.write_bytes(b"synthetic-media")
    # A media path must never be read as one large bytes/base64 object.
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("whole-file read")))
    first = await fm.upload_file_path(path, "clip.mp4")
    second = await fm.upload_file_path(path, "again.mp4")
    assert first["local_id"] == second["local_id"]
    fm._uploader.upload_path.assert_awaited_once()
    block = {"type": "video", "local_id": first["local_id"], "mime": "video/mp4"}
    assert await fm.resolve_remote_file_id(block, ("gcs",)) == first["file_id"]
    await fm.cleanup_session_files([{"content": [block]}])
    fm._uploader.delete.assert_not_awaited()
    assert await fm.delete_file(first["local_id"])
    fm._uploader.delete.assert_awaited_once_with(first["file_id"])
    assert not (tmp_path / first["path"]).exists()


async def test_media_refresh_and_temporary_cleanup(fm, tmp_path):
    path = tmp_path / "video"
    path.write_bytes(b"synthetic-media")
    record = await fm.upload_file_path(path)
    fm._uploader.persistent = False
    block = {"type": "video", "local_id": record["local_id"]}
    history = [{"role": "user", "content": [block]}]
    await fm.cleanup_session_files(history)
    await fm.ensure_fresh_file_ids(history)
    assert fm._uploader.upload_path.await_count == 2
    assert block["file_id"] == record["file_id"]


async def test_download_single_part_cleanup_and_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(media.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(media.shutil, "which", lambda name: name)
    manager = SimpleNamespace(configured=True, upload_file_path=AsyncMock(return_value={"local_id": "downloaded"}))
    paths = []
    async def download(args, **kwargs):
        assert args[-1] == "https://www.bilibili.com/video/BV123?p=2"
        assert args[args.index("--concurrent-fragments") + 1] == "1"
        assert args[args.index("--retries") + 1] == "0"
        assert kwargs["timeout"] == 180
        assert kwargs["max_disk_bytes"] == media.MAX_MEDIA_BYTES * 2
        path = Path(kwargs["directory"]) / "video.mp4"
        path.write_bytes(b"synthetic video")
        paths.append(path)
    monkeypatch.setattr(media, "run_program", download)
    assert await media.fetch_bilibili("https://bilibili.com/video/BV123?p=2", manager) == {"local_id": "downloaded"}
    manager.upload_file_path.assert_awaited_once()
    assert not paths[0].parent.exists()


@pytest.mark.parametrize("cancel", [False, True])
async def test_process_timeout_and_cancellation_reap_child(monkeypatch, cancel):
    process = SimpleNamespace(pid=123, returncode=None)
    done = asyncio.Event()
    async def wait():
        await done.wait()
        process.returncode = -9
    process.wait = wait
    kill = Mock(side_effect=lambda *args: done.set())
    killer = SimpleNamespace(wait=AsyncMock(side_effect=lambda: done.set()))
    create = AsyncMock(side_effect=[process, killer])
    monkeypatch.setattr(media.os, "killpg", kill, raising=False)
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(media.run_program(["synthetic-program"], timeout=0.02))
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ValueError, match="时间上限"):
            await task
    if media.os.name == "nt":
        assert create.call_args.args[:3] == ("taskkill", "/PID", "123")
    else:
        kill.assert_called_once()
    assert process.returncode == -9


@pytest.mark.parametrize("style", ["interactions", "gemini_generate"])
@pytest.mark.parametrize("kind", ["audio", "video"])
async def test_gemini_wire_format(style, kind):
    cfg = ModelConfig("gemini-test", "https://synthetic", api_style=style, auth="adc", audio=True, video=True)
    mime = "audio/mp3" if kind == "audio" else "video/mp4"
    resolver = AsyncMock(return_value="gs://synthetic/clip")
    context = Context("", [], "", "test", image_file_resolver=resolver)
    body = await get_dialect(style)._request(cfg, [{"role": "user", "content": [
        {"type": kind, "mime": mime, "local_id": "local", "file_id": "old-vendor-id"}]}], None, context)
    part = body["input"][0]["content"][0] if style == "interactions" else body["contents"][0]["parts"][0]
    expected = {"type": kind, "uri": "gs://synthetic/clip", "mime_type": mime} if style == "interactions" else {
        "fileData": {"fileUri": "gs://synthetic/clip", "mimeType": mime}}
    assert part == expected
    resolver.assert_awaited_once()


async def test_missing_remote_media_fails_explicitly():
    cfg = ModelConfig("gemini", "https://synthetic", api_style="interactions", audio=True)
    with pytest.raises(ValueError, match="远端引用"):
        await get_dialect("interactions")._media({"type": "audio", "mime": "audio/mp3"}, cfg, Context("", [], "", "test"))


async def test_gemini_file_processing_is_ready_before_return(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"synthetic video")
    pending = {"name": "files/abc", "state": "PROCESSING"}
    ready = {"name": "files/abc", "state": "ACTIVE", "uri": "https://generativelanguage.googleapis.com/v1beta/files/abc",
             "expirationTime": "2030-01-01T00:00:00Z"}
    def response(payload=None, headers=None):
        return Mock(__aenter__=AsyncMock(return_value=SimpleNamespace(
            raise_for_status=Mock(), json=AsyncMock(return_value=payload), headers=headers or {})), __aexit__=AsyncMock())
    requests = Mock(post=Mock(side_effect=[response(headers={"X-Goog-Upload-URL": "https://upload.synthetic"}),
        response({"file": pending})]), get=Mock(return_value=response(ready)))
    monkeypatch.setattr(files.aiohttp, "ClientSession", Mock(return_value=Mock(
        __aenter__=AsyncMock(return_value=requests), __aexit__=AsyncMock())))
    monkeypatch.setattr(files.asyncio, "sleep", AsyncMock())
    uploader = files.GeminiFilesUploader("https://generativelanguage.googleapis.com/v1beta", "synthetic", 3600)
    uri, expires = await uploader.upload_path(path, "clip.mp4", "video/mp4")
    assert uri == ready["uri"] and expires > time.time()
    assert requests.post.call_args_list[0].kwargs["headers"]["X-Goog-Upload-Header-Content-Length"] == str(path.stat().st_size)
    assert hasattr(requests.post.call_args_list[1].kwargs["data"], "read")
    requests.get.assert_called_once()


async def test_gcs_media_upload_streams_with_content_addressed_uri(tmp_path, monkeypatch):
    import hashlib
    path = tmp_path / "clip"
    path.write_bytes(b"synthetic media")
    request = Mock(__aenter__=AsyncMock(return_value=SimpleNamespace(status=200)), __aexit__=AsyncMock())
    session = Mock(post=Mock(return_value=request))
    monkeypatch.setattr(files.aiohttp, "ClientSession", Mock(return_value=Mock(
        __aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())))
    uploader = object.__new__(files.GCSFilesUploader)
    uploader.bucket, uploader.prefix, uploader.proxy = "synthetic", "media", None
    uploader._headers = AsyncMock(return_value={"authorization": "synthetic"})
    uri, _ = await uploader.upload_path(path, "clip.mp4", "video/mp4")
    assert uri == f"gs://synthetic/media/{hashlib.sha256(b'synthetic media').hexdigest()}.mp4"
    assert hasattr(session.post.call_args.kwargs["data"], "read")
    assert session.post.call_args.kwargs["headers"]["Content-Type"] == "video/mp4"


@pytest.mark.parametrize("failed", [False, True])
async def test_shared_session_links_archival_and_failure_notes(tmp_path, monkeypatch, failed):
    agent = AgentSession.__new__(AgentSession)
    agent._lock = asyncio.Lock()
    agent.platform = "qq"
    agent._system_prompt = "synthetic"
    agent.callback = AsyncMock()
    download_config = {"backend": "http_jobs", "base_url": "https://example.invalid"}
    agent.manager = SimpleNamespace(snapshot=lambda: {"media_inputs": {"download": download_config}})
    session = SimpleNamespace(session_id="synthetic", start_time=1, history=[])
    agent.session_manager = SimpleNamespace(maybe_compress_daily=AsyncMock(),
        get_or_create_active_session=lambda: session, record_input_time=Mock(), context_tokens=0)
    agent._loop = SimpleNamespace(model_config=ModelConfig("gemini", "https://synthetic", api_style="interactions", video=True),
        run=AsyncMock(side_effect=lambda messages, context: LoopResult(
            full_messages=messages, final_answer="answer", total_usage={})))
    agent._update_history_and_archive = AsyncMock()
    agent._save_rollback_state = Mock()
    manager = SimpleNamespace(configured=False)
    monkeypatch.setattr(sessions, "FileManager", lambda: manager)
    record = {"local_id": "video-id", "mime": "video/mp4", "filename": "clip.mp4", "path": "synthetic"}
    fetch = AsyncMock(side_effect=ValueError("synthetic download failure")) if failed else AsyncMock(return_value=record)
    monkeypatch.setattr(sessions, "fetch_bilibili", fetch)
    await agent.run_round("看看 https://b23.tv/abc")
    fetch.assert_awaited_once_with("https://b23.tv/abc", manager, download_config)
    content = agent._loop.run.call_args.args[0][-1]["content"]
    archive = agent._update_history_and_archive.call_args.args[2][0]
    if failed:
        assert "没有获取" in content
        assert "synthetic download failure" in archive["content"]
    else:
        assert content[1] == {"type": "text", "text": "[file:synthetic]"}
        assert content[2]["type"] == "video"
        assert content[2]["mime"] == "video/mp4"
        assert "[file:synthetic]" in archive["content"]
        assert archive["images"][0]["local_id"] == "video-id"
    assert agent._pending_user_rounds == 0
    terminal = agent.callback.call_args.args[0]
    assert terminal.type == EventType.ROUND_FINISHED
    assert terminal.data["status"] == "completed"
    assert terminal.data["stop_reason"] == "complete"


async def test_http_upload_send_and_missing_attachment(fm, tmp_path, monkeypatch):
    from web.adapter import ConnectionManager
    from web.server import create_app
    from web.settings import WebSettings
    import web.server as server
    settings = WebSettings(str(tmp_path / "settings.json"))
    agent = SimpleNamespace(run_round=AsyncMock(return_value=SimpleNamespace(total_usage={}, stop_reason="complete")))
    adapter = SimpleNamespace(heartbeat=None)
    monkeypatch.setattr(server, "get_file_manager", lambda: fm)
    app = create_app(ConnectionManager(), adapter, agent, None, None,
        SimpleNamespace(db_path=tmp_path / "unused.db"), settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic") as client:
        response = await client.post("/api/files/upload", files={"file": ("clip.mp4", b"synthetic-video", "video/mp4")})
        assert response.status_code == 200, response.text
        local_id = response.json()["file"]["local_id"]
        response = await client.post("/api/chat/send", json={"text": "", "files": [local_id]})
        assert response.status_code == 200
        assert agent.run_round.call_args.kwargs["images"][0]["mime"] == "video/mp4"
        response = await client.post("/api/chat/send", json={"text": "hello", "files": ["missing"]})
        assert response.status_code == 400
        settings.set_password("synthetic-password")
        response = await client.post("/api/files/upload", files={"file": ("clip.mp4", b"video")})
        assert response.status_code == 401
