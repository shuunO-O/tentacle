#!/usr/bin/env python3
"""确认目标提交已在 main 分支完成并通过 CI；不再跨 workflow 搬运证明附件。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def api_pages(endpoint):
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError('无法查询 CI 状态；请检查 Actions 读取权限和远端状态')
    return json.loads(result.stdout)


def select_evidence(repository, commit):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
        raise ValueError('GitHub 仓库名格式不符')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('候选须为完整提交 SHA')
    base = f'repos/{repository}/actions'
    pages = api_pages(f'{base}/workflows/ci.yml/runs?head_sha={commit}&branch=main&per_page=100')
    runs = [run for page in pages for run in page['workflow_runs']
            if run.get('head_sha') == commit and run.get('head_branch') == 'main'
            and run.get('event') in {'push', 'workflow_dispatch'}
            and (run.get('head_repository') or {}).get('full_name', '').lower() == repository.lower()]
    if not runs:
        raise ValueError('该提交缺少 main CI；请先在 main 为该提交运行 CI，再重跑 Release')
    # 不回退到旧的成功记录，避免掩盖较新的失败、取消或尚未完成的验证。
    run = max(runs, key=lambda item: item['id'])
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        raise ValueError('该提交最新 main CI 尚未完整通过；请先完成或修复 CI，再重跑 Release')
    return {'run_id': run['id'], 'head_sha': commit, 'status': 'success'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('commit')
    args = parser.parse_args(argv)
    try:
        selected = select_evidence(args.repository, args.commit)
        if os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
                for name, value in selected.items():
                    output.write(f'{name}={value}\n')
        print(json.dumps(selected))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
