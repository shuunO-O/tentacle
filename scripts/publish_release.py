#!/usr/bin/env python3
"""将已校验的正式版本附件发布到 GitHub Release；不部署、不覆盖现有 Release。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

if __package__:
    from . import release_package
else:
    import release_package


def gh(*args):
    # 不回显 gh 的 stderr、认证环境或远端响应正文。
    result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError('GitHub 操作失败；保留已有状态，不自动覆盖或删除，请检查 Actions 权限/远端状态')
    return result.stdout


def remote_tag(repository, identity):
    ref = json.loads(gh('api', f'repos/{repository}/git/ref/tags/{identity["version"]}'))
    obj = ref.get('object', {})
    if obj.get('type') != 'tag' or obj.get('sha') != identity['tag_object']:
        raise ValueError('GitHub 标签对象与目标版本不符')
    tag = json.loads(gh('api', f'repos/{repository}/git/tags/{identity["tag_object"]}'))
    if tag.get('object', {}).get('type') != 'commit' or tag['object'].get('sha') != identity['commit']:
        raise ValueError('GitHub 标签提交与目标版本不符')


def publish(repo, directory, repository, *, draft=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
        raise ValueError('GitHub 仓库名格式不符')
    checked = release_package.check(directory)
    if checked['kind'] != 'release':
        raise ValueError('候选预览包不能上传为正式 Release')
    identity = release_package.resolve_tag(repo, checked['version'])
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    if any(manifest[key] != value for key, value in identity.items()):
        raise ValueError('附件版本与本地标签不符')
    remote_tag(repository, identity)
    assets = [str(path.absolute()) for path in sorted(directory.iterdir())]
    # --verify-tag 禁止隐式创建标签；existing Release 会失败，不使用 --clobber。
    flags = ['--draft'] if draft else ['--latest']
    gh('release', 'create', identity['version'], *assets, '--repo', repository,
       '--verify-tag', *flags, '--title', 'Tentacle ' + identity['version'],
       '--notes-file', str((directory / 'RELEASE_NOTES.md').absolute()))
    remote_tag(repository, identity)
    if draft:
        pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repository}/releases?per_page=100'))
        matching = [item for page in pages for item in page if item.get('tag_name') == identity['version']]
        if len(matching) != 1:
            raise ValueError('未找到唯一的上传草稿')
        release = matching[0]
    else:
        release = json.loads(gh('api', f'repos/{repository}/releases/tags/{identity["version"]}'))
    uploaded = {asset['name']: asset for asset in release.get('assets', [])}
    if release.get('draft') is not draft or release.get('tag_name') != identity['version'] or set(uploaded) != {p.name for p in directory.iterdir()}:
        raise ValueError('Release 状态或上传附件集合不符')
    for path in directory.iterdir():
        asset = uploaded[path.name]
        if (asset.get('state') != 'uploaded' or asset.get('size') != path.stat().st_size
                or asset.get('digest') != 'sha256:' + release_package.digest(path.read_bytes())):
            raise ValueError('GitHub 附件校验值不符')
    return {'ok': True, 'draft': draft, **identity, 'assets': len(uploaded), 'url': release['html_url']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--draft', action='store_true', help='仅创建草稿 Release，不立即设为正式版')
    args = parser.parse_args(argv)
    try:
        if not args.repository:
            raise ValueError('须指定 --repository 或 GITHUB_REPOSITORY')
        result = publish(args.source.resolve(), args.directory, args.repository, draft=args.draft)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
