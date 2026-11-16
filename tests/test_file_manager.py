"""
FileManager 厂商无关铺垫单元测试（tmp_path 临时库，不碰生产 data/、不触网）

覆盖：
- 上传策略接口：FakeUploader 顶替远端，验证 FileManager 与策略的协作
- local_id 驱动的 ensure_fresh_file_ids：过期重传、旧格式块（仅 file_id）
  反查补齐 local_id、同一记录只刷新一次
- file_base_url 签发端点不符（换厂商）触发重传
- base64 供给：image_block_to_data_url / get_image_data_url
- cleanup_session_files 的 local_id 驱动清理
"""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

import attachments.manager as file_manager_module
from attachments.manager import FileManager

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeUploader:
    """假上传策略：记录调用，返回固定 file_id"""

    name = "fake"
    persistent = False

    def __init__(self, file_id="file-fake-1", fail=False):
        self.uploads = []
        self.deletes = []
        self.file_id = file_id
        self.fail = fail
        self.base_url = "https://vendor.example.com"

    async def upload(self, data, filename, mime):
        if self.fail:
            raise RuntimeError("上传失败")
        self.uploads.append((filename, mime))
        return self.file_id, time.time() + 7 * 24 * 3600

    async def delete(self, file_id):
        self.deletes.append(file_id)


@pytest.fixture(autouse=True)
def reset_file_manager_singleton(monkeypatch):
    """重置单例，并让纯单元测试不依赖默认线程池的关闭行为。"""
    async def run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)
    original = FileManager._instance
    FileManager._instance = None
    yield
    FileManager._instance = original


@pytest.fixture
def fm(tmp_path, monkeypatch):
    """指向临时库的 FileManager（默认策略已被 FakeUploader 顶替，不触网）"""
    manager = FileManager(str(tmp_path / "chronicle.db"))
    monkeypatch.setenv("TENTACLE_TEST_FILES_KEY", "sk-test")
    manager.configure({
        "base_url": "https://vendor.example.com",
        "api_key_env": "TENTACLE_TEST_FILES_KEY",
    })
    manager._uploader = FakeUploader()
    manager._uploaders = {"fake": manager._uploader}
    return manager


def make_local_image(tmp_path: Path, name: str = "l-1.png") -> Path:
    """写一张本地图片（绝对路径直接作为 record.path，读回时绕过 PROJECT_ROOT 拼接）"""
    path = tmp_path / name
    path.write_bytes(PNG_BYTES)
    return path


def test_missing_google_adc_leaves_default_gcs_unavailable(fm, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    def missing_adc():
        raise DefaultCredentialsError("Synthetic credential details")

    monkeypatch.setattr('config.google_auth.load_credentials', missing_adc)
    fm.configure({"uploader": "gcs", "base_url": "gs://synthetic-bucket/images"})

    assert not fm.configured
    assert fm._uploader is None and "gcs" not in fm._uploaders


def insert_record(fm, path: Path, local_id="l-1", file_id="file-old-1",
                  expires_in=48 * 3600, remote_deleted=0):
    record = {
        "local_id": local_id,
        "filename": Path(path).name,
        "path": str(path),
        "mime": "image/png",
        "size": len(PNG_BYTES),
        "sha256": f"sha-{local_id}",
        "file_id": file_id,
        "uploaded_time": time.time(),
        "expires_at": time.time() + expires_in,
        "remote_deleted": remote_deleted,
        "created_time": time.time(),
    }
    fm._db_insert(record)
    return record


def set_file_base_url(fm, local_id: str, base_url: str):
    """直接设置签发端点（_db_insert 不含该列，模拟历史记录）"""
    with sqlite3.connect(fm.db_path) as conn:
        conn.execute("UPDATE files SET file_base_url = ? WHERE local_id = ?", (base_url, local_id))


# ==================== 上传策略协作 ====================


async def test_upload_image_uses_uploader(fm):
    record = await fm.upload_image(PNG_BYTES, "a.png")
    assert record["file_id"] == "file-fake-1"
    assert fm._uploader.uploads == [("a.png", "image/png")]


async def test_upload_image_dedup_reuses_record(fm, tmp_path):
    first = await fm.upload_image(PNG_BYTES, "a.png")
    second = await fm.upload_image(PNG_BYTES, "b.png")
    assert first["local_id"] == second["local_id"]
    # 未过期时去重路径不重传
    assert len(fm._uploader.uploads) == 1


async def test_remote_refs_are_provider_scoped_and_reused(fm):
    record = await fm.upload_image(PNG_BYTES, "a.png")
    block = {"type": "image", "local_id": record["local_id"], "file_id": "gs://wrong"}

    assert await fm.resolve_remote_file_id(block, ("fake",)) == "file-fake-1"
    assert await fm.resolve_remote_file_id(block, ("deepseek_files",)) is None
    assert len(fm._uploader.uploads) == 1


async def test_model_connection_controls_upload_and_account_scoped_reuse(fm, monkeypatch):
    from dataclasses import replace
    from agent.protocol import ModelConfig
    created = []

    class AccountUploader(FakeUploader):
        name = 'deepseek_files'
        def __init__(self, base_url, key, expires, proxy=None):
            super().__init__(file_id='file-' + key)
            self.base_url = base_url
            self.key = key
            created.append(self)

    monkeypatch.setitem(fm._UPLOADERS, 'deepseek_files', AccountUploader)
    image = await fm.upload_image(PNG_BYTES, 'a.png')
    block = {'type': 'image', 'local_id': image['local_id'], 'file_id': 'gs://foreign'}
    cfg = ModelConfig('deepseek-test', 'https://same.test', api_key='account-a', provider='deepseek', provider_id='a')
    assert await fm.resolve_remote_file_id(block, ('deepseek_files',), model_config=cfg) == 'file-account-a'
    other = replace(cfg, api_key='account-b', provider_id='b')
    assert await fm.resolve_remote_file_id(block, ('deepseek_files',), model_config=other) == 'file-account-b'
    assert await fm.resolve_remote_file_id(block, ('deepseek_files',), model_config=cfg) == 'file-account-a'
    assert [len(u.uploads) for u in created] == [1, 1]
    assert (await fm.get_file(image['local_id']))['file_id'] == 'file-fake-1'
    refs = fm._db_list_remote_refs(image['local_id'])
    assert len([r for r in refs if r['uploader'] == 'deepseek_files']) == 2
    assert 'account-a' not in str([r['connection_scope'] for r in refs])
    assert 'account-b' not in str([r['connection_scope'] for r in refs])


def test_legacy_remote_refs_migrate_without_assuming_account_ownership(fm):
    with sqlite3.connect(fm.db_path) as conn:
        conn.execute('DROP TABLE file_remote_refs')
        conn.execute('''CREATE TABLE file_remote_refs (
            local_id TEXT, uploader TEXT, base_url TEXT, remote_id TEXT,
            uploaded_time REAL, expires_at REAL, remote_deleted INTEGER,
            PRIMARY KEY (local_id,uploader,base_url))''')
        conn.execute("INSERT INTO file_remote_refs VALUES ('old','fake','https://old','id',1,2,0)")
    fm._init_db()
    ref = fm._db_get_remote_ref('old', 'fake', 'https://old')
    assert ref['remote_id'] == 'id' and ref['connection_scope'] == ''


async def test_upload_image_rejects_invalid_format(fm):
    with pytest.raises(ValueError):
        await fm.upload_image(b"not-an-image", "x.png")


async def test_upload_image_requires_configuration(tmp_path, monkeypatch):
    manager = FileManager(str(tmp_path / "b.db"))
    manager.configure({"base_url": "https://vendor.example.com", "api_key_env": "TENTACLE_TEST_FILES_KEY"})
    monkeypatch.delenv("TENTACLE_TEST_FILES_KEY", raising=False)
    manager.configure({"base_url": "https://vendor.example.com", "api_key_env": "TENTACLE_TEST_FILES_KEY"})
    assert manager.configured is False
    with pytest.raises(ValueError):
        await manager.upload_image(PNG_BYTES, "a.png")


def test_configure_rejects_unknown_uploader(tmp_path, monkeypatch):
    manager = FileManager(str(tmp_path / "c.db"))
    monkeypatch.setenv("TENTACLE_TEST_FILES_KEY", "sk-test")
    with pytest.raises(ValueError):
        manager.configure({
            "base_url": "https://vendor.example.com",
            "api_key_env": "TENTACLE_TEST_FILES_KEY",
            "uploader": "no_such_strategy",
        })


# ==================== ensure_fresh_file_ids（local_id 驱动） ====================


async def test_ensure_fresh_reuploads_stale(fm, tmp_path):
    """临期记录：从本地副本重传，file_id 原地替换，local_id 不变"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=3600)  # 1h < 24h 刷新阈值 -> stale
    history = [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    block = history[0]["content"][1]
    assert block["file_id"] == "file-fake-1"
    assert block["local_id"] == "l-1"
    assert len(fm._uploader.uploads) == 1


async def test_ensure_fresh_skips_fresh_record(fm, tmp_path):
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=10 * 24 * 3600)
    set_file_base_url(fm, "l-1", "https://vendor.example.com")
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert history[0]["content"][0]["file_id"] == "file-old-1"
    assert fm._uploader.uploads == []


async def test_ensure_fresh_null_base_url_treated_stale(fm, tmp_path):
    """旧库迁移（file_base_url 为 NULL）：视为签发端点未知，一次性重传补齐"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=10 * 24 * 3600)  # 未过期但 file_base_url 为 NULL
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert len(fm._uploader.uploads) == 1
    assert history[0]["content"][0]["file_id"] == "file-fake-1"
    record = await fm.get_file("l-1")
    assert record["file_base_url"] == "https://vendor.example.com"


async def test_ensure_fresh_backfills_local_id_for_legacy_block(fm, tmp_path):
    """旧格式块（仅 file_id）：按 DB 反查补齐 local_id"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=10 * 24 * 3600)
    set_file_base_url(fm, "l-1", "https://vendor.example.com")
    history = [{"role": "user", "content": [
        {"type": "image", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert history[0]["content"][0]["local_id"] == "l-1"
    assert fm._uploader.uploads == []


async def test_ensure_fresh_vendor_switch_reuploads(fm, tmp_path):
    """签发端点与当前 files.base_url 不符（换厂商）：未过期也重传"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=10 * 24 * 3600)
    set_file_base_url(fm, "l-1", "https://old-vendor.example.com")
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert history[0]["content"][0]["file_id"] == "file-fake-1"
    assert len(fm._uploader.uploads) == 1
    # 重传后签发端点已更新为当前厂商
    record = await fm.get_file("l-1")
    assert record["file_base_url"] == "https://vendor.example.com"


async def test_ensure_fresh_same_record_refreshed_once(fm, tmp_path):
    """多块引用同一图片：只重传一次，所有块同步更新"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=3600)
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert len(fm._uploader.uploads) == 1
    for block in history[0]["content"]:
        assert block["file_id"] == "file-fake-1"


async def test_ensure_fresh_refresh_failure_keeps_old_file_id(fm, tmp_path):
    """重传失败：块保留旧 file_id（方言层可再走 base64 兜底）"""
    path = make_local_image(tmp_path)
    insert_record(fm, path, expires_in=3600)
    fm._uploader = FakeUploader(fail=True)
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.ensure_fresh_file_ids(history)
    assert history[0]["content"][0]["file_id"] == "file-old-1"


async def test_ensure_fresh_unconfigured_noop(tmp_path):
    manager = FileManager(str(tmp_path / "u.db"))
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "f-1"},
    ]}]
    await manager.ensure_fresh_file_ids(history)
    assert history[0]["content"][0] == {"type": "image", "local_id": "l-1", "file_id": "f-1"}


# ==================== base64 供给 ====================


async def test_image_block_to_data_url(fm, tmp_path):
    path = make_local_image(tmp_path)
    insert_record(fm, path, file_id=None)
    import base64

    data_url = await fm.image_block_to_data_url({"type": "image", "local_id": "l-1"})
    assert data_url == f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}"


async def test_image_block_to_data_url_resolves_legacy_block_by_file_id(fm, tmp_path):
    """旧格式块（无 local_id）：按 file_id 反查记录后供给 base64"""
    path = make_local_image(tmp_path)
    insert_record(fm, path)
    data_url = await fm.image_block_to_data_url({"type": "image", "file_id": "file-old-1"})
    assert data_url is not None
    assert data_url.startswith("data:image/png;base64,")


async def test_image_block_to_data_url_missing_returns_none(fm):
    assert await fm.image_block_to_data_url({"type": "image", "local_id": "nope"}) is None
    assert await fm.image_block_to_data_url({"type": "image"}) is None


async def test_legacy_id_lookup_refuses_ambiguous_cross_vendor_ids(fm, tmp_path):
    first = make_local_image(tmp_path, 'first.png')
    second = make_local_image(tmp_path, 'second.png')
    insert_record(fm, first, local_id='first', file_id='collision')
    insert_record(fm, second, local_id='second', file_id='collision')
    assert await fm.image_block_to_data_url({'type': 'image', 'file_id': 'collision'}) is None
    assert await fm.image_block_to_data_url({'type': 'image', 'local_id': 'first'}) is not None


async def test_get_image_data_url_missing_local_file(fm, tmp_path):
    """记录在但本地文件丢失：返回 None 不抛错"""
    insert_record(fm, tmp_path / "ghost.png")
    assert await fm.get_image_data_url("l-1") is None


# ==================== cleanup_session_files ====================


async def test_cleanup_marks_remote_deleted(fm, tmp_path):
    path = make_local_image(tmp_path)
    insert_record(fm, path)
    set_file_base_url(fm, 'l-1', fm._base_url)
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.cleanup_session_files(history)
    assert fm._uploader.deletes == ["file-old-1"]
    record = await fm.get_file("l-1")
    assert record["remote_deleted"] == 1
    # 本地副本保留
    assert path.exists()


async def test_cleanup_does_not_delete_legacy_ref_with_unknown_owner(fm, tmp_path):
    insert_record(fm, make_local_image(tmp_path))
    await fm.cleanup_session_files([{'role': 'user', 'content': [{'type': 'image', 'local_id': 'l-1'}]}])
    assert fm._uploader.deletes == []
    assert (await fm.get_file('l-1'))['remote_deleted'] == 0


async def test_cleanup_skips_already_deleted(fm, tmp_path):
    path = make_local_image(tmp_path)
    insert_record(fm, path, remote_deleted=1)
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]
    await fm.cleanup_session_files(history)
    assert fm._uploader.deletes == []


async def test_cleanup_preserves_persistent_remote_ref(fm, tmp_path):
    path = make_local_image(tmp_path)
    insert_record(fm, path)
    fm._uploader.persistent = True
    history = [{"role": "user", "content": [
        {"type": "image", "local_id": "l-1", "file_id": "file-old-1"},
    ]}]

    await fm.cleanup_session_files(history)

    assert fm._uploader.deletes == []
    assert (await fm.get_file("l-1"))["remote_deleted"] == 0


async def test_vertex_gcs_refs_are_isolated_by_identity_and_bucket(fm, monkeypatch):
    from dataclasses import replace
    from agent.protocol import ModelConfig
    created = []

    class ScopedGCS(FakeUploader):
        name = 'gcs'
        def __init__(self, endpoint, key, expires, proxy=None, *, credentials_env='', project=None):
            super().__init__(file_id=endpoint + '/' + credentials_env)
            self.base_url = endpoint
            self.credentials_env = credentials_env
            created.append(self)

    monkeypatch.setattr(file_manager_module, 'GCSFilesUploader', ScopedGCS)
    fm._defer_uploads = True
    image = await fm.upload_image(PNG_BYTES, 'synthetic.png')
    assert image['file_id'] is None
    assert fm._uploader.uploads == []
    block = {'type': 'image', 'local_id': image['local_id']}
    cfg = ModelConfig('test', 'https://test', auth='adc', provider_id='a', project='a',
                      credentials_env='SYNTHETIC_A', gcs_base_url='gs://synthetic-a/images')
    other = replace(cfg, provider_id='b', credentials_env='SYNTHETIC_B', gcs_base_url='gs://synthetic-b/images')
    assert await fm.resolve_remote_file_id(block, ('gcs',), model_config=cfg) == 'gs://synthetic-a/images/SYNTHETIC_A'
    assert await fm.resolve_remote_file_id(block, ('gcs',), model_config=other) == 'gs://synthetic-b/images/SYNTHETIC_B'
    assert await fm.resolve_remote_file_id(block, ('gcs',), model_config=cfg) == 'gs://synthetic-a/images/SYNTHETIC_A'
    assert [len(u.uploads) for u in created] == [1, 1]
    # 即使使用相同 bucket，账号不同也不能复用签发记录。
    same_bucket = replace(other, gcs_base_url=cfg.gcs_base_url)
    assert await fm.resolve_remote_file_id(block, ('gcs',), model_config=same_bucket) == 'gs://synthetic-a/images/SYNTHETIC_B'
    assert len(created) == 3
    assert fm._target_uploader('gcs', replace(cfg, gcs_base_url='')) is None


async def test_scoped_ingress_needs_no_global_adc(fm, monkeypatch):
    def no_global_adc(*args, **kwargs):
        raise AssertionError('global ADC must not be loaded')
    monkeypatch.setattr('google.auth.default', no_global_adc)
    fm.configure({'uploader': 'gcs', 'base_url': 'gs://synthetic-legacy/images', 'defer_uploads': True})
    image = await fm.upload_image(PNG_BYTES, 'local.png')
    assert image['file_id'] is None
    assert fm.configured
