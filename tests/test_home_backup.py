"""TENTACLE_HOME 定期备份、一次性加密导出、两步导入恢复及路由/CLI 测试。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import httpx
import ladybug
import pytest

from runtime import home_backup as hb
from runtime.control import RestartController
from scripts import backup_home as cli
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import WebSettings


@dataclass
class _StubSession:
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _restarting: bool = False
    chronicle: object = None


def _make_home(root: Path, *, message: str = "synthetic-hello", env_secret: str = "sk-synthetic-secret-123") -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "prompt").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "custom" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "harmony").mkdir(parents=True, exist_ok=True)
    (root / "data" / "files" / "2026-11").mkdir(parents=True, exist_ok=True)
    (root / "data" / "plugins" / "harmony").mkdir(parents=True, exist_ok=True)
    (root / "data" / "self_backups").mkdir(parents=True, exist_ok=True)
    (root / "data" / "config_backups").mkdir(parents=True, exist_ok=True)
    (root / "data" / "dsh").mkdir(parents=True, exist_ok=True)
    (root / "certs").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "repo").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)

    (root / "config" / "config.yaml").write_text(
        "config_version: 2\n"
        "providers:\n"
        "  syn:\n"
        "    type: openai_compatible\n"
        "    base_url: https://example.invalid/v1\n"
        "    api_key_env: SYNTHETIC_API_KEY\n"
        "models:\n"
        "  syn/test:\n"
        "    provider: syn\n"
        "    name: test-model\n"
        "roles:\n"
        "  chat:\n"
        "    model: syn/test\n",
        encoding="utf-8",
    )
    (root / "config" / ".env").write_text(f"SYNTHETIC_API_KEY='{env_secret}'\n", encoding="utf-8")
    (root / "certs" / "server.key").write_text("PRIVATE-KEY-DO-NOT-BACKUP", encoding="utf-8")
    (root / "prompt" / "soul.md").write_text("synthetic soul prompt", encoding="utf-8")
    (root / "skills" / "demo" / "SKILL.md").write_text("# Demo Skill", encoding="utf-8")
    (root / "plugins" / "custom" / "plugin.py").write_text("PLUGIN_VER = 1\n", encoding="utf-8")
    (root / "plugins" / "custom" / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"\x00\x01")
    (root / "data" / "files" / "2026-11" / "id1_note.txt").write_text("attachment body", encoding="utf-8")
    (root / "data" / "plugins" / "qqbot" / "chat.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "data" / "plugins" / "qqbot" / "chat.json").write_text('{"openid":"synthetic"}', encoding="utf-8")
    (root / "data" / "webui_settings.json").write_text('{"show_reasoning": true, "split_messages": true, "password_hash": "", "password_salt": "", "device_auth": false, "device_tokens": {}}', encoding="utf-8")
    (root / "data" / "device_access_policy.json").write_text('{"allow_new_devices": false}', encoding="utf-8")
    (root / "data" / "device_auth_freeze.json").write_text('{"created": 1}', encoding="utf-8")
    (root / "data" / "self_backups" / "old.txt").write_text("self backup", encoding="utf-8")
    (root / "data" / "config_backups" / "old.yaml").write_text("old: 1", encoding="utf-8")
    (root / "data" / "dsh" / "scratch.txt").write_text("dsh", encoding="utf-8")
    (root / "data" / "gemini_explicit_cache.json").write_text('{"cached": 1}', encoding="utf-8")
    (root / "workspace" / "repo" / "main.py").write_text("print('workspace untouched')", encoding="utf-8")
    (root / "logs" / "agent.log").write_text("log line", encoding="utf-8")
    (root / "tmp" / "scratch.md").write_text("tmp line", encoding="utf-8")

    # SQLite in WAL mode (retain an active connection during snapshot!)
    chronicle = root / "data" / "chronicle.db"
    conn = sqlite3.connect(chronicle)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO raw_messages(content) VALUES (?)", (message,))
    conn.commit()
    conn.close()

    plugin_db = root / "data" / "plugins" / "harmony" / "phone.db"
    p_conn = sqlite3.connect(plugin_db)
    p_conn.execute("CREATE TABLE devices (id TEXT PRIMARY KEY)")
    p_conn.execute("INSERT INTO devices(id) VALUES ('phone-1')")
    p_conn.commit()
    p_conn.close()

    # Ladybug DB with pending WAL frame before CHECKPOINT
    lbdb_path = root / "data" / "memory.lbdb"
    db = ladybug.Database(str(lbdb_path))
    lconn = ladybug.Connection(db)
    lconn.execute("CREATE NODE TABLE Event(id STRING PRIMARY KEY, summary STRING)")
    lconn.execute("CREATE (:Event {id: 'e1', summary: $msg})", {"msg": message})
    lconn.close()
    db.close()

    return root


def _tar_names(archive_path: Path) -> set[str]:
    with tarfile.open(archive_path, "r:gz") as tar:
        return set(tar.getnames())


async def test_backup_scope_excludes_secrets_workspace_and_snapshots_live_wal_and_ladybug(tmp_path):
    home = _make_home(tmp_path / "home")
    # Open a WAL writer keeping an uncheckpointed transaction visible cleanly via sqlite backup
    wal_conn = sqlite3.connect(home / "data" / "chronicle.db")
    wal_conn.execute("PRAGMA journal_mode=WAL")
    wal_conn.execute("INSERT INTO raw_messages(content) VALUES ('wal-only-row')")
    wal_conn.commit()

    # Also open Ladybug in live driver so _snapshot_ladybug tests active driver CHECKPOINT
    from memory.ladybug_driver import LadybugDriver

    driver = LadybugDriver(home / "data" / "memory.lbdb", 4)
    await driver.ready()
    async with driver.session() as sess:
        await sess.run("CREATE (:Event {id: 'e2', summary: 'live-ladybug-node'})")
    assert (home / "data" / "memory.lbdb.wal").exists()
    fake_store = SimpleNamespace(driver=driver)

    mgr = hb.HomeBackupManager(home, ladybug_store=fake_store)
    try:
        meta = await mgr.create_backup(kind="auto", include_files=True)
    finally:
        wal_conn.close()
        await driver.close()

    arc = mgr.get_backup_archive_path(meta["backup_id"])
    names = _tar_names(arc)
    assert "manifest.json" in names
    assert "payload/config/config.yaml" in names
    assert "payload/prompt/soul.md" in names
    assert "payload/skills/demo/SKILL.md" in names
    assert "payload/plugins/custom/plugin.py" in names
    assert "payload/data/chronicle.db" in names
    assert "payload/data/memory.lbdb" in names
    assert "payload/data/plugins/harmony/phone.db" in names
    assert "payload/data/files/2026-11/id1_note.txt" in names
    assert "payload/data/plugins/qqbot/chat.json" in names

    # Must NEVER leak .env, certs, auth files, workspace, logs, tmp, __pycache__, or WAL sidecars
    forbidden = {
        "payload/config/.env",
        "payload/certs/server.key",
        "payload/data/webui_settings.json",
        "payload/data/device_access_policy.json",
        "payload/data/device_auth_freeze.json",
        "payload/data/self_backups/old.txt",
        "payload/data/config_backups/old.yaml",
        "payload/data/dsh/scratch.txt",
        "payload/data/gemini_explicit_cache.json",
        "payload/workspace/repo/main.py",
        "payload/logs/agent.log",
        "payload/tmp/scratch.md",
        "payload/plugins/custom/__pycache__/plugin.cpython-313.pyc",
        "payload/data/chronicle.db-wal",
        "payload/data/chronicle.db-shm",
        "payload/data/memory.lbdb.wal",
    }
    assert not (forbidden & names)

    # Verify extracted chronicle.db & memory.lbdb contain the live WAL writes cleanly
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    with tarfile.open(arc, "r:gz") as tar:
        tar.extractall(verify_dir, filter="data")
    with closing(sqlite3.connect(verify_dir / "payload/data/chronicle.db")) as vconn:
        rows = [r[0] for r in vconn.execute("SELECT content FROM raw_messages ORDER BY id").fetchall()]
    assert rows == ["synthetic-hello", "wal-only-row"]
    vdb = ladybug.Database(str(verify_dir / "payload/data/memory.lbdb"), read_only=True)
    try:
        with closing(ladybug.Connection(vdb)) as vlc:
            ids = [r[0] for r in vlc.execute("MATCH (e:Event) RETURN e.id ORDER BY e.id").get_all()]
        assert ids == ["e1", "e2"]
    finally:
        vdb.close()

    # Check include_files=False omits data/files/
    no_files_meta = await mgr.create_backup(kind="manual", include_files=False)
    no_files_names = _tar_names(mgr.get_backup_archive_path(no_files_meta["backup_id"]))
    assert "payload/data/files/2026-11/id1_note.txt" not in no_files_names
    assert no_files_meta["includes_files"] is False


async def test_auto_retention_prunes_only_auto_backups_and_preserves_manual(tmp_path):
    home = _make_home(tmp_path / "home")
    mgr = hb.HomeBackupManager(home)
    mgr.update_policy(retention_count=2)

    manual = await mgr.create_backup(kind="manual")
    auto1 = await mgr.create_backup(kind="auto")
    await asyncio.sleep(0.02)
    auto2 = await mgr.create_backup(kind="auto")
    await asyncio.sleep(0.02)
    auto3 = await mgr.create_backup(kind="auto")

    ids = {item["backup_id"] for item in hb.list_backups_sync(home)}
    assert manual["backup_id"] in ids
    assert auto1["backup_id"] not in ids
    assert {auto2["backup_id"], auto3["backup_id"]} <= ids


async def test_encrypted_ephemeral_export_and_import_apply_with_auth_and_workspace_preserved(tmp_path):
    src_home = _make_home(tmp_path / "src_home", message="from-source-machine", env_secret="sk-migrated-env-999")
    src_mgr = hb.HomeBackupManager(src_home)

    # 加密一次性导出（含 .env），不得落在 data/backups/
    tmp, export_path, _ = await src_mgr.create_ephemeral_export(
        include_files=True,
        include_env=True,
        passphrase="correct-passphrase",
    )
    try:
        assert not (src_home / "data" / "backups").exists()
        raw_blob = export_path.read_bytes()
        assert raw_blob.startswith(hb.ENCRYPTED_MAGIC)
        assert b"sk-migrated-env-999" not in raw_blob

        target_home = _make_home(tmp_path / "target_home", message="old-target-msg", env_secret="sk-old-target-111")
        (target_home / "data" / "webui_settings.json").write_text('{"keep":"target-auth"}', encoding="utf-8")
        (target_home / "workspace" / "repo" / "main.py").write_text("target-workspace-stays", encoding="utf-8")
        # Plant stale WAL sidecar on target that must be removed on restore
        (target_home / "data" / "memory.lbdb.wal").write_bytes(b"stale-wal-garbage")

        tgt_mgr = hb.HomeBackupManager(target_home)
        with pytest.raises(hb.PassphraseRequiredError):
            await tgt_mgr.inspect_import(archive_path=export_path)
        with pytest.raises(hb.BackupError, match="口令错误"):
            await tgt_mgr.inspect_import(archive_path=export_path, passphrase="wrong-passphrase")

        # Truncated ciphertext must also fail
        truncated = tmp_path / "truncated.tbackup"
        truncated.write_bytes(raw_blob[:-4])
        with pytest.raises(hb.BackupError):
            await tgt_mgr.inspect_import(archive_path=truncated, passphrase="correct-passphrase")

        preview = await tgt_mgr.inspect_import(archive_path=export_path, passphrase="correct-passphrase")
        assert preview["encrypted"] is True
        assert preview["includes_env"] is True
        assert preview["env_key_count"] == 1
        assert preview["has_files"] is True

        await tgt_mgr.prepare_apply_import(
            preview["import_token"],
            apply_files=True,
            apply_env=True,
            create_pre_backup=True,
        )
    finally:
        tmp.cleanup()

    result = hb.apply_pending_restore(target_home)
    assert result is not None and result["status"] == "succeeded"
    assert result["applied_env"] is True
    assert result["pre_backup_id"]
    assert not (target_home / hb.PENDING_APPLY_REL).exists()
    assert not (target_home / "data" / "memory.lbdb.wal").exists()

    # 验证数据与加密 .env 均已迁入，且目标机鉴权和 workspace 原封不动
    assert "sk-migrated-env-999" in (target_home / "config" / ".env").read_text(encoding="utf-8")
    assert json.loads((target_home / "data" / "webui_settings.json").read_text(encoding="utf-8")) == {"keep": "target-auth"}
    assert (target_home / "workspace" / "repo" / "main.py").read_text(encoding="utf-8") == "target-workspace-stays"
    with closing(sqlite3.connect(target_home / "data" / "chronicle.db")) as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "from-source-machine"


@pytest.mark.parametrize("attack", ["traversal", "symlink", "auth_file", "epoch", "tampered_sha", "plain_env"])
async def test_import_rejects_malicious_or_incompatible_archives(tmp_path, attack):
    home = _make_home(tmp_path / "home")
    mgr = hb.HomeBackupManager(home)
    meta = await mgr.create_backup(kind="manual")
    good_arc = mgr.get_backup_archive_path(meta["backup_id"])

    entries: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(good_arc, "r:gz") as tar:
        for member in tar.getmembers():
            data = tar.extractfile(member).read() if member.isfile() else b""
            entries.append((member, data))

    manifest_idx = next(i for i, (m, _) in enumerate(entries) if m.name == "manifest.json")
    manifest = json.loads(entries[manifest_idx][1].decode("utf-8"))

    if attack == "epoch":
        manifest["data_epoch"] = 999
    elif attack == "tampered_sha":
        idx = next(i for i, (m, _) in enumerate(entries) if m.name == "payload/prompt/soul.md")
        m, _ = entries[idx]
        tampered = b"tampered content"
        m.size = len(tampered)
        entries[idx] = (m, tampered)
    elif attack == "traversal":
        ti = tarfile.TarInfo("../evil.txt")
        ti.size = 4
        entries.append((ti, b"evil"))
    elif attack == "symlink":
        ti = tarfile.TarInfo("payload/prompt/link.md")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        entries.append((ti, b""))
    elif attack == "auth_file":
        ti = tarfile.TarInfo("payload/data/webui_settings.json")
        ti.size = 2
        entries.append((ti, b"{}"))
        manifest["files"].append({"path": "payload/data/webui_settings.json", "size": 2, "sha256": hb._sha256_file.__name__})
    elif attack == "plain_env":
        manifest["includes_env"] = True

    manifest_bytes = json.dumps(manifest).encode("utf-8")
    m_info, _ = entries[manifest_idx]
    m_info.size = len(manifest_bytes)
    entries[manifest_idx] = (m_info, manifest_bytes)

    bad_arc = tmp_path / f"{attack}.tar.gz"
    with tarfile.open(bad_arc, "w:gz") as tar:
        for member, data in entries:
            tar.addfile(member, io.BytesIO(data) if member.isfile() else None)

    with pytest.raises(hb.BackupError):
        await mgr.inspect_import(archive_path=bad_arc)


async def test_web_routes_enforce_auth_on_env_export_and_never_echo_passphrase(tmp_path):
    home = _make_home(tmp_path / "home")
    (home / "data" / "device_access_policy.json").unlink()
    (home / "data" / "device_auth_freeze.json").unlink()
    settings = WebSettings(str(home / "data" / "webui_settings.json"))
    session = _StubSession()
    restart = RestartController(session)
    mgr = hb.HomeBackupManager(home)
    app = create_app(
        ConnectionManager(),
        SimpleNamespace(get_terminal_history=lambda since=0: [], get_usage_stats=lambda: {}, chat_state=None),
        session,
        None,
        None,
        SimpleNamespace(db_path=home / "data" / "chronicle.db", get_active_session=lambda: None, get_recent_rounds=lambda *a: []),
        settings,
        SimpleNamespace(),
        restart_controller=restart,
        backup_manager=mgr,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        # 无鉴权时允许常规状态查询与政策更新，但严禁带 .env 导出
        status = (await client.get("/api/backups")).json()
        assert status["env_export_allowed"] is False
        assert status["restart_available"] is True

        upd = await client.put("/api/backups/policy", json={"interval_hours": 12, "retention_count": 5})
        assert upd.status_code == 200 and upd.json()["policy"]["interval_hours"] == 12

        now_res = await client.post("/api/backups/now", json={})
        assert now_res.status_code == 200
        bid = now_res.json()["backup"]["backup_id"]

        dl_res = await client.get(f"/api/backups/{bid}/download")
        assert dl_res.status_code == 200 and "no-store" in dl_res.headers["cache-control"]

        forbidden = await client.post("/api/backups/export", json={
            "include_env": True,
            "passphrase": "secret-passphrase-123",
        })
        assert forbidden.status_code == 403
        assert "secret-passphrase-123" not in forbidden.text

        # 开启密码后允许一次性加密导出 .env，但短口令 422 绝不回显口令
        settings.set_password("admin-pw")
        token = settings.issue_token()
        headers = {"Authorization": f"Bearer {token}"}

        short_pw = await client.post(
            "/api/backups/export",
            headers=headers,
            json={"include_env": True, "passphrase": "shortpw"},
        )
        assert short_pw.status_code == 422
        assert "shortpw" not in short_pw.text

        ok_export = await client.post(
            "/api/backups/export",
            headers=headers,
            json={"include_env": True, "passphrase": "strong-passphrase-99"},
        )
        assert ok_export.status_code == 200
        assert ok_export.content.startswith(hb.ENCRYPTED_MAGIC)
        assert b"sk-synthetic-secret-123" not in ok_export.content
        assert "no-store" in ok_export.headers["cache-control"]

        # 验证服务端 data/backups 里没有任何 .env 或 .tbackup 留存
        assert list((home / "data" / "backups").glob("*.tbackup")) == []

        # 上传加密导出包：未传口令返回 422 + code=passphrase_required，传错/正确分别校验
        need_pw = await client.post(
            "/api/backups/import/inspect",
            headers=headers,
            files={"file": ("migration.tbackup", ok_export.content, "application/octet-stream")},
        )
        assert need_pw.status_code == 422 and need_pw.json()["code"] == "passphrase_required"

        preview = await client.post(
            "/api/backups/import/inspect",
            headers=headers,
            files={"file": ("migration.tbackup", ok_export.content, "application/octet-stream")},
            data={"passphrase": "strong-passphrase-99"},
        )
        assert preview.status_code == 200
        import_token = preview.json()["import_token"]

        applied = await client.post(
            "/api/backups/import/apply",
            headers=headers,
            json={"import_token": import_token, "apply_env": True, "apply_files": True, "create_pre_backup": True},
        )
        assert applied.status_code == 202
        assert (home / hb.PENDING_APPLY_REL).is_file()
        await asyncio.sleep(0)
        assert restart.ready.is_set()
        await restart.close()


def test_cli_export_and_offline_import_roundtrip(tmp_path):
    src = _make_home(tmp_path / "cli_src", message="cli-msg", env_secret="sk-cli-777")
    dst = _make_home(tmp_path / "cli_dst", message="old-dst", env_secret="sk-dst-old")
    pw_file = tmp_path / "pw.txt"
    pw_file.write_text("cli-passphrase-88\n", encoding="utf-8")
    out_arc = tmp_path / "cli-export.tbackup"

    assert cli.main(["--home", str(src), "export", "-o", str(out_arc), "--include-env", "--passphrase-file", str(pw_file)]) == 0
    assert out_arc.is_file()
    assert cli.main(["--home", str(dst), "import", str(out_arc), "--include-env", "--passphrase-file", str(pw_file), "--apply"]) == 0
    assert "sk-cli-777" in (dst / "config" / ".env").read_text(encoding="utf-8")
    with closing(sqlite3.connect(dst / "data" / "chronicle.db")) as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "cli-msg"


async def test_apply_pending_restore_rolls_back_on_mid_swap_error(tmp_path, monkeypatch):
    src = _make_home(tmp_path / "src", message="new-msg")
    dst = _make_home(tmp_path / "dst", message="original-msg")
    (dst / "prompt" / "soul.md").write_text("original soul", encoding="utf-8")

    src_mgr = hb.HomeBackupManager(src)
    meta = await src_mgr.create_backup(kind="manual")
    dst_mgr = hb.HomeBackupManager(dst)
    preview = await dst_mgr.inspect_import(archive_path=src_mgr.get_backup_archive_path(meta["backup_id"]))
    await dst_mgr.prepare_apply_import(preview["import_token"], create_pre_backup=False)

    orig_move = hb._move_or_copy
    calls = 0

    def failing_copy(s: Path, d: Path):
        nonlocal calls
        if "payload" in s.parts:
            calls += 1
            if calls >= 3:
                raise OSError("simulated disk error")
        return orig_move(s, d)

    monkeypatch.setattr(hb, "_move_or_copy", failing_copy)
    result = hb.apply_pending_restore(dst)
    monkeypatch.setattr(hb, "_move_or_copy", orig_move)

    assert result is not None and result["status"] == "failed"
    assert not (dst / hb.PENDING_APPLY_REL).exists()
    assert (dst / "prompt" / "soul.md").read_text(encoding="utf-8") == "original soul"
    with closing(sqlite3.connect(dst / "data" / "chronicle.db")) as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "original-msg"


async def test_auto_scheduler_tick_respects_interval(tmp_path):
    home = _make_home(tmp_path / "home")
    mgr = hb.HomeBackupManager(home)
    mgr.update_policy(enabled=True, interval_hours=24, retention_count=3)

    await mgr._run_auto_tick()
    snap1 = mgr.status_snapshot()
    assert snap1["policy"]["last_status"] == "succeeded"
    assert len(snap1["backups"]) == 1

    # 间隔未到时不应重复触发
    await mgr._run_auto_tick()
    assert len(mgr.status_snapshot()["backups"]) == 1


async def test_manual_selective_backup_with_workspace_paths_and_partial_restore(tmp_path):
    src = _make_home(tmp_path / "src", message="src-msg")
    (src / "prompt" / "soul.md").write_text("updated prompt soul", encoding="utf-8")
    (src / "workspace" / "deliverables").mkdir(parents=True, exist_ok=True)
    (src / "workspace" / "deliverables" / "report.md").write_text("# Agent Deliverable", encoding="utf-8")
    (src / "workspace" / "deliverables" / ".env").write_text("SECRET=skip-me", encoding="utf-8")
    (src / "workspace" / ".git").mkdir(parents=True, exist_ok=True)
    (src / "workspace" / ".git" / "config").write_text("git-config", encoding="utf-8")
    (src / "workspace" / "unselected.txt").write_text("do not include", encoding="utf-8")

    src_mgr = hb.HomeBackupManager(src)
    tree = src_mgr.list_workspace("")
    names_in_root = {item["name"] for item in tree["entries"]}
    assert "deliverables" in names_in_root
    assert ".git" not in names_in_root

    sub_tree = src_mgr.list_workspace("deliverables")
    assert [item["name"] for item in sub_tree["entries"]] == ["report.md"]

    with pytest.raises(hb.BackupError):
        src_mgr.list_workspace("../config")

    # 局部手动备份：仅勾选 prompt 与 workspace/deliverables（并故意传重叠路径检验去重）
    meta = await src_mgr.create_backup(
        kind="manual",
        include_files=False,
        scopes=["prompt"],
        workspace_paths=["deliverables", "deliverables/report.md"],
    )
    assert meta["included_scopes"] == ["prompt"]
    assert meta["includes_workspace"] is True
    assert meta["workspace_file_count"] == 1
    names = _tar_names(src_mgr.get_backup_archive_path(meta["backup_id"]))
    assert "payload/prompt/soul.md" in names
    assert "payload/workspace/deliverables/report.md" in names
    assert "payload/workspace/deliverables/.env" not in names
    assert "payload/workspace/unselected.txt" not in names
    assert "payload/data/chronicle.db" not in names
    assert "payload/config/config.yaml" not in names

    # 目标机恢复局部备份：prompt 和 workspace/deliverables 更新，但 chronicle.db / config / skills / workspace 其他项目完全保留
    dst = _make_home(tmp_path / "dst", message="keep-target-msg")
    (dst / "skills" / "demo" / "SKILL.md").write_text("keep-target-skill", encoding="utf-8")
    (dst / "workspace" / "other_project").mkdir(parents=True, exist_ok=True)
    (dst / "workspace" / "other_project" / "keep.py").write_text("keep-me", encoding="utf-8")

    dst_mgr = hb.HomeBackupManager(dst)
    preview = await dst_mgr.inspect_import(archive_path=src_mgr.get_backup_archive_path(meta["backup_id"]))
    assert preview["included_scopes"] == ["prompt"]
    assert preview["has_workspace"] is True
    assert preview["workspace_sample_paths"] == ["deliverables/report.md"]

    await dst_mgr.prepare_apply_import(
        preview["import_token"],
        apply_files=False,
        apply_env=False,
        apply_workspace=True,
        create_pre_backup=True,
    )
    result = hb.apply_pending_restore(dst)
    assert result is not None and result["status"] == "succeeded"
    assert result["applied_workspace"] is True
    assert result["applied_scopes"] == ["prompt"]

    assert (dst / "prompt" / "soul.md").read_text(encoding="utf-8") == "updated prompt soul"
    assert (dst / "workspace" / "deliverables" / "report.md").read_text(encoding="utf-8") == "# Agent Deliverable"
    assert (dst / "workspace" / "other_project" / "keep.py").read_text(encoding="utf-8") == "keep-me"
    assert (dst / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "keep-target-skill"
    with closing(sqlite3.connect(dst / "data" / "chronicle.db")) as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "keep-target-msg"


async def test_web_workspace_browse_and_selective_manual_backup_routes(tmp_path):
    home = _make_home(tmp_path / "home")
    (home / "data" / "device_access_policy.json").unlink()
    (home / "data" / "device_auth_freeze.json").unlink()
    (home / "workspace" / "reports").mkdir(parents=True, exist_ok=True)
    (home / "workspace" / "reports" / "q1.md").write_text("Q1 report", encoding="utf-8")
    settings = WebSettings(str(home / "data" / "webui_settings.json"))
    session = _StubSession()
    mgr = hb.HomeBackupManager(home)
    app = create_app(
        ConnectionManager(),
        SimpleNamespace(get_terminal_history=lambda since=0: [], get_usage_stats=lambda: {}, chat_state=None),
        session,
        None,
        None,
        SimpleNamespace(db_path=home / "data" / "chronicle.db", get_active_session=lambda: None, get_recent_rounds=lambda *a: []),
        settings,
        SimpleNamespace(),
        restart_controller=RestartController(session),
        backup_manager=mgr,
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        root = await client.get("/api/backups/workspace")
        assert root.status_code == 200
        assert any(e["name"] == "reports" and e["is_dir"] for e in root.json()["entries"])

        sub = await client.get("/api/backups/workspace", params={"path": "reports"})
        assert sub.status_code == 200
        assert [e["path"] for e in sub.json()["entries"]] == ["reports/q1.md"]

        now_res = await client.post(
            "/api/backups/now",
            json={"include_files": False, "scopes": ["prompt"], "workspace_paths": ["reports/q1.md"]},
        )
        assert now_res.status_code == 200
        bk = now_res.json()["backup"]
        assert bk["included_scopes"] == ["prompt"]
        assert bk["includes_workspace"] is True
        assert bk["workspace_file_count"] == 1

        exp_res = await client.post(
            "/api/backups/export",
            json={"include_files": False, "scopes": [], "workspace_paths": ["reports/q1.md"]},
        )
        assert exp_res.status_code == 200
        exp_arc = tmp_path / "ws-only.tar.gz"
        exp_arc.write_bytes(exp_res.content)
        assert _tar_names(exp_arc) == {"manifest.json", "payload/workspace/reports/q1.md"}


def test_cli_selective_scope_and_workspace_roundtrip(tmp_path):
    src = _make_home(tmp_path / "cli_sel_src", message="src-only")
    dst = _make_home(tmp_path / "cli_sel_dst", message="dst-keep")
    (src / "workspace" / "artifact.txt").write_text("deliverable-42", encoding="utf-8")
    out_arc = tmp_path / "cli-partial.tar.gz"

    assert cli.main([
        "--home", str(src), "export", "-o", str(out_arc),
        "--no-files", "--scope", "prompt", "--workspace-path", "artifact.txt",
    ]) == 0
    assert cli.main(["--home", str(dst), "import", str(out_arc), "--apply"]) == 0
    assert (dst / "workspace" / "artifact.txt").read_text(encoding="utf-8") == "deliverable-42"
    with closing(sqlite3.connect(dst / "data" / "chronicle.db")) as conn:
        assert conn.execute("SELECT content FROM raw_messages").fetchone()[0] == "dst-keep"


def test_collect_referenced_env_vars_includes_enabled_bundled_plugin_env():
    disabled_cfg = {"plugins": {"items": {"qqbot": {"enabled": False, "config": {}}}}}
    assert "QQ_APP_ID" not in hb.collect_referenced_env_vars(disabled_cfg)

    enabled_cfg = {
        "plugins": {
            "items": {
                "qqbot": {
                    "enabled": True,
                    "config": {
                        "app_id_env": "CUSTOM_QQ_APP",
                        "client_secret_env": "CUSTOM_QQ_SECRET",
                    },
                }
            }
        }
    }
    referenced = hb.collect_referenced_env_vars(enabled_cfg)
    assert "CUSTOM_QQ_APP" in referenced and "CUSTOM_QQ_SECRET" in referenced
