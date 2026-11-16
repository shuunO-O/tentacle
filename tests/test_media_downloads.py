"""Real loopback HTTP transport, synthetic bytes; no credentials or live sites."""

import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web
import pytest

from config.store import ConfigStore, ConfigurationError
from attachments.downloads import DownloadSettings, HttpJobsDownloader, MediaDownloadError, download_settings
import attachments.media as media_inputs

DATA = b"synthetic-mp4-not-a-real-video"
READY = {
    "job_id": "job-1",
    "state": "ready",
    "expires_at": "2099-01-01T00:00:00Z",
    "source": {"provider": "bilibili", "video_id": "BV123", "page": 1},
    "media": {
        "mime": "video/mp4",
        "filename": "video.mp4",
        "size_bytes": len(DATA),
        "duration_seconds": 2,
        "width": 640,
        "height": 360,
        "sha256": hashlib.sha256(DATA).hexdigest(),
    },
}


@pytest.fixture
async def worker():
    state = SimpleNamespace(
        health={"protocol": 1, "ready": True, "busy": False},
        ready=deepcopy(READY),
        mode="",
        requests=[],
        posts=[],
        transfers=0,
        deletes=0,
        content_entered=asyncio.Event(),
        unblock=asyncio.Event(),
    )

    async def route(request):
        state.requests.append((request.method, request.path))
        if request.path == "/health":
            if state.mode == "oversized_json":
                return web.json_response({"padding": "x" * 65536})
            if state.mode == "redirect":
                return web.Response(status=302, headers={"Location": "/never-follow"})
            return web.json_response(state.health)
        if request.path == "/jobs" and request.method == "POST":
            state.posts.append(
                {
                    "key": request.headers["Idempotency-Key"],
                    "body": await request.json(),
                    "authorization": request.headers.get("Authorization"),
                }
            )
            if state.mode == "creation_reply_lost" and len(state.posts) == 1:
                request.transport.close()
                return web.Response()
            if state.mode == "worker_busy":
                return web.json_response({"error": "worker_busy"}, status=429)
            if state.mode == "untrusted_error":
                return web.json_response({"error": {"path": "/private/secret"}}, status=500)
            value = (
                {"job_id": "job-1", "state": "downloading", "poll_after_seconds": 1}
                if state.mode == "poll"
                else state.ready
            )
            return web.json_response(value, status=202)
        if request.method == "DELETE":
            state.deletes += 1
            return web.Response(status=500 if state.mode == "cleanup_failed" else 204)
        if request.path == "/jobs/job-1":
            return web.json_response(state.ready)
        if request.path == "/jobs/job-1/content":
            state.transfers += 1
            state.content_entered.set()
            if state.mode == "blocked":
                await state.unblock.wait()
            if state.mode in ("truncated_once", "truncated_always") and (
                state.transfers == 1 or state.mode == "truncated_always"
            ):
                response = web.StreamResponse(headers={"Content-Length": str(len(DATA))})
                await response.prepare(request)
                await response.write(DATA[:3])
                request.transport.close()
                return response
            if state.mode == "length_mismatch":
                return web.Response(body=DATA + b"x", content_type="video/mp4")
            if state.mode == "encoded":
                return web.Response(body=DATA, headers={"Content-Encoding": "gzip"})
            if state.mode == "missing_length":
                response = web.StreamResponse()
                response.enable_chunked_encoding()
                await response.prepare(request)
                await response.write(DATA)
                await response.write_eof()
                return response
            return web.Response(body=DATA, content_type="video/mp4")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state.settings = DownloadSettings(backend="http_jobs", base_url=f"http://127.0.0.1:{port}")
    yield state
    state.unblock.set()
    await runner.cleanup()


async def test_remote_short_link_received_unchanged_and_job_kept_until_upload(worker, tmp_path, monkeypatch):
    monkeypatch.setattr(
        media_inputs, "canonical_bilibili_url", AsyncMock(side_effect=AssertionError("cloud resolution"))
    )
    monkeypatch.setattr(media_inputs, "run_program", AsyncMock(side_effect=AssertionError("local downloader")))
    monkeypatch.setattr(media_inputs.importlib.util, "find_spec", Mock(side_effect=AssertionError("yt-dlp dependency")))
    seen = []

    async def upload(path, filename):
        assert worker.deletes == 0
        assert Path(path).read_bytes() == DATA
        seen.append(Path(path))
        return {"local_id": "accepted"}

    files = SimpleNamespace(configured=True, upload_file_path=upload)
    result = await media_inputs.fetch_bilibili("https://b23.tv/abc", files, vars(worker.settings))
    assert result == {"local_id": "accepted"}
    assert worker.posts[0]["body"] == {"url": "https://b23.tv/abc", "profile": "bilibili-480p-v1"}
    assert worker.deletes == 1
    assert not seen[0].parent.exists()


@pytest.mark.parametrize(
    "mode,transfers,posts",
    [
        ("", 1, 1),
        ("truncated_once", 2, 1),
        ("creation_reply_lost", 1, 2),
        ("poll", 1, 1),
    ],
)
async def test_success_and_idempotent_recovery(worker, tmp_path, mode, transfers, posts):
    worker.mode = mode
    path = tmp_path / "video.mp4"
    async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", path):
        assert path.read_bytes() == DATA
        assert worker.deletes == 0
    assert worker.transfers == transfers
    assert len(worker.posts) == posts
    assert len({item["key"] for item in worker.posts}) == 1
    assert worker.deletes == 1
    assert not path.with_suffix(".part").exists()


@pytest.mark.parametrize("mode", ["truncated_always", "length_mismatch", "encoded", "missing_length"])
async def test_bad_transfer_never_promoted(worker, tmp_path, mode):
    worker.mode = mode
    path = tmp_path / "video.mp4"
    with pytest.raises(MediaDownloadError):
        async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", path):
            pytest.fail("Unverified content reached consumer")
    assert not path.exists()
    assert not path.with_suffix(".part").exists()
    assert worker.deletes == 1
    assert worker.transfers == (2 if mode == "truncated_always" else 1)
    assert len(worker.posts) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("sha256", "0" * 64),
        ("sha256", "invalid"),
        ("size_bytes", 64 * 1024 * 1024 + 1),
        ("size_bytes", True),
        ("duration_seconds", 601),
        ("duration_seconds", float("nan")),
        ("height", 481),
        ("mime", "application/octet-stream"),
    ],
)
async def test_invalid_metadata_or_checksum_rejected(worker, tmp_path, key, value):
    worker.ready["media"][key] = value
    path = tmp_path / "video.mp4"
    with pytest.raises(MediaDownloadError):
        async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", path):
            pytest.fail("Invalid media reached consumer")
    assert not path.exists()
    assert worker.deletes == 1


@pytest.mark.parametrize("mode", ["worker_busy", "redirect", "oversized_json", "untrusted_error"])
async def test_control_failures_bounded_and_sanitized(worker, tmp_path, mode):
    worker.mode = mode
    with pytest.raises(MediaDownloadError) as caught:
        async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", tmp_path / "video.mp4"):
            pytest.fail("Control failure reached consumer")
    assert "/private/secret" not in str(caught.value)
    assert worker.transfers == 0
    assert not any(path == "/never-follow" for _, path in worker.requests)


@pytest.mark.parametrize(
    "health",
    [
        {"protocol": 2, "ready": True, "busy": False},
        {"protocol": 1, "ready": False, "busy": False},
        {"protocol": 1, "ready": True, "busy": True},
    ],
)
async def test_unavailable_worker_does_not_create_or_fallback(worker, tmp_path, health):
    worker.health = health
    with pytest.raises(MediaDownloadError):
        async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", tmp_path / "video.mp4"):
            pass
    assert worker.posts == []


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancellation_or_total_deadline_cleans_job_and_partial(worker, tmp_path, cancel):
    worker.mode = "blocked"
    path = tmp_path / "video.mp4"
    settings = DownloadSettings(**{**vars(worker.settings), "timeout_seconds": 1 if not cancel else 10})

    async def work():
        async with HttpJobsDownloader(settings).download("https://b23.tv/abc", path):
            pytest.fail("Blocked transfer completed")

    task = asyncio.create_task(work())
    await asyncio.wait_for(worker.content_entered.wait(), 5)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else MediaDownloadError):
        await task
    assert worker.deletes == 1
    assert not path.exists()
    assert not path.with_suffix(".part").exists()


async def test_consumer_failure_still_cleans_and_preserves_original_error(worker, tmp_path):
    with pytest.raises(RuntimeError, match="upload failed"):
        async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", tmp_path / "video.mp4"):
            raise RuntimeError("upload failed")
    assert worker.deletes == 1


async def test_cleanup_failure_does_not_discard_verified_download(worker, tmp_path, caplog):
    worker.mode = "cleanup_failed"
    path = tmp_path / "video.mp4"
    async with HttpJobsDownloader(worker.settings).download("https://b23.tv/abc", path):
        assert path.read_bytes() == DATA
    assert "TTL" in caplog.text


async def test_optional_auth_and_environment_proxies_are_not_implicitly_used(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SYNTHETIC_MEDIA_TOKEN", "test-only-token")
    settings = DownloadSettings(**{**vars(worker.settings), "api_key_env": "SYNTHETIC_MEDIA_TOKEN"})
    async with HttpJobsDownloader(settings).download("https://b23.tv/abc", tmp_path / "video.mp4"):
        pass
    assert worker.posts[0]["authorization"] == "Bearer test-only-token"


@pytest.mark.parametrize(
    "value",
    [
        {"backend": "unknown"},
        {"backend": "http_jobs"},
        {"base_url": "https://user:secret@worker.test/"},
        {"base_url": "https://worker.test/?token=secret"},
        {"base_url": "https://worker.test/#fragment"},
        {"base_url": []},
        {"timeout_seconds": True},
        {"timeout_seconds": 361},
        {"profile": "../other"},
        {"api_key_env": "secret-value-here"},
        {"unknown": True},
    ],
)
def test_download_config_validation(value):
    with pytest.raises(ValueError):
        download_settings(value)


def test_config_store_exposes_and_validates_backend_atomically(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("providers: {}\nmodels: {}\nroles: {}\n# retained\n")
    store = ConfigStore(path)
    revision, _, _ = store.read()
    with pytest.raises(ConfigurationError):
        store.preview({'media_inputs.download.backend': "http_jobs"}, revision)
    result = store.save(
        {
            'media_inputs.download.backend': "http_jobs",
            'media_inputs.download.base_url': "https://worker.example/api/v1/media",
        },
        revision,
    )
    assert len(result["changes"]) == 2
    assert "# retained" in path.read_text()
    _, _, raw = store.read()
    assert download_settings(raw["media_inputs"]["download"]).backend == "http_jobs"
    store.save(
        {
            'media_inputs.download.backend': "local",
            'media_inputs.download.base_url': "",
            'media_inputs.download.proxy': "",
            'media_inputs.download.api_key_env': "",
        },
        result["revision"],
    )
    _, _, raw = store.read()
    assert download_settings(raw["media_inputs"]["download"]).backend == "local"


@pytest.mark.parametrize('state', ['failed', 'cancelled'])
async def test_terminal_worker_failure_is_cleaned_without_transfer(worker, tmp_path, state):
    worker.ready.update(state=state, error='upstream_rejected', upstream_status=412)
    with pytest.raises(MediaDownloadError) as caught:
        async with HttpJobsDownloader(worker.settings).download('https://b23.tv/abc', tmp_path / 'video.mp4'):
            pytest.fail('Failed job reached consumer')
    if state == 'failed':
        assert 'upstream_rejected' in str(caught.value) and '412' in str(caught.value)
    assert worker.deletes == 1 and worker.transfers == 0


@pytest.mark.parametrize('url', ['https://www.bilibili.com/other', 'https://b23.tv/a/b',
                                'https://www.bilibili.com/video/BV123?p=0',
                                'https://user:pass@www.bilibili.com/video/BV123'])
async def test_invalid_source_rejected_before_worker_or_local_network(worker, url):
    files = SimpleNamespace(configured=True, upload_file_path=AsyncMock())
    with pytest.raises(ValueError):
        await media_inputs.fetch_bilibili(url, files, vars(worker.settings))
    assert worker.requests == []
    files.upload_file_path.assert_not_awaited()
