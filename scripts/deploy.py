#!/usr/bin/env python3
"""Shared filesystem and Git primitives for Linux release tooling."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import stat
import subprocess
import tempfile


# 已移除模块的旧目录仍属于受管路径，供升级删除旧文件及回退时校验使用。
CODE_DIRS = {'agent', 'attachments', 'config', 'runtime', 'dialects', 'memory', 'plugins', 'qqbot', 'subconscious', 'subagents', 'tools', 'web', 'scripts'}
CODE_SUFFIXES = {'.py', '.sh', '.js', '.css', '.html', '.svg', '.png', '.ico', '.woff', '.woff2'}
ROOT_FILES = {'start.sh', 'requirements.txt', 'requirements-dev.txt', 'pyproject.toml', 'config.defaults.yaml'}
UPDATER_FILES = (
    'deploy.py',
    'release_package.py',
    'update_runtime.py',
    'github_releases.py',
    'update.py',
    'check_updates.py',
    'maintenance.py',
    'install_update.py',
)


def managed(name):
    p = PurePosixPath(name)
    if not p.parts or p.is_absolute() or '..' in p.parts or any(x.startswith('.') for x in p.parts):
        return False
    if p.parts[0] == 'plugins' and len(p.parts) >= 3 and p.parts[1] == 'bundled':
        return p.suffix in CODE_SUFFIXES | {'.yaml', '.md'}
    return (len(p.parts) == 1 and (p.suffix == '.py' or name in ROOT_FILES)) or (
        p.parts[0] in CODE_DIRS and p.suffix in CODE_SUFFIXES
    )


def git(repo, *args):
    # 管理员明确选定的源码目录；兼容以生产用户读取 ubuntu 的检出目录。
    return subprocess.check_output(
        ['git', '-c', 'pack.threads=1', '-c', f'safe.directory={repo}', '-C', str(repo), *args],
        stderr=subprocess.PIPE, timeout=60
    )


def revision(repo, ref):
    return git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()


def checked_path(root, name):
    p = root
    for part in PurePosixPath(name).parts:
        p = p / part
        if p.is_symlink():
            raise ValueError(f'拒绝生产符号链接：{name}')
    if p.exists() and not p.is_file():
        raise ValueError(f'生产目标不是普通文件：{name}')
    return p


def fingerprint(data, mode):
    if data is None:
        return None
    return {'sha256': hashlib.sha256(data).hexdigest(), 'executable': bool(mode & 0o111)}


def current(root, name):
    p = checked_path(root, name)
    if not p.exists():
        return None, 0
    return p.read_bytes(), stat.S_IMODE(p.stat().st_mode)


def write_file(path, data, mode, owner=None):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if owner is not None and os.geteuid() == 0:
        for parent in reversed(missing):
            os.chown(parent, *owner)
    fd, tmp = tempfile.mkstemp(prefix='.deploy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if owner is not None and os.geteuid() == 0:
                os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def deployment_lock(backups):
    """Serialize release installation, switching and recovery."""
    import fcntl

    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    with checked_path(backups, '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
