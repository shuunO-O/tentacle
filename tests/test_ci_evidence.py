"""CI 状态校验使用合成 API 响应；不联网、不读取真实运行数据。"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import ci_evidence


COMMIT = 'a' * 40
REPOSITORY = 'example/repo'


def run(**changes):
    return {'id': 20, 'head_sha': COMMIT, 'head_branch': 'main', 'event': 'push',
            'head_repository': {'full_name': REPOSITORY},
            'status': 'completed', 'conclusion': 'success', **changes}


class CIEvidenceTests(unittest.TestCase):
    def select(self, runs):
        with patch.object(ci_evidence, 'api_pages', return_value=runs) as api:
            result = ci_evidence.select_evidence(REPOSITORY, COMMIT)
        self.assertIn(f'head_sha={COMMIT}&branch=main', api.call_args_list[0].args[0])
        self.assertEqual(api.call_count, 1)
        return result

    def test_exact_main_commit_succeeds_across_pages(self):
        result = self.select([{'workflow_runs': []}, {'workflow_runs': [run()]}])
        self.assertEqual(result, {'run_id': 20, 'head_sha': COMMIT, 'status': 'success'})

    def test_manual_main_ci_can_authorize_release(self):
        result = self.select([{'workflow_runs': [run(event='workflow_dispatch')]}])
        self.assertEqual(result['run_id'], 20)

    def test_other_commits_branches_repositories_and_prs_are_rejected(self):
        for changes in ({'head_sha': 'b' * 40}, {'head_branch': 'feature'},
                        {'event': 'pull_request'}, {'head_repository': {'full_name': 'other/repo'}},
                        {'head_repository': {}}, {'head_repository': None}):
            with self.subTest(changes=changes), patch.object(
                ci_evidence, 'api_pages', return_value=[{'workflow_runs': [run(**changes)]}],
            ) as api:
                with self.assertRaisesRegex(ValueError, '缺少 main CI'):
                    ci_evidence.select_evidence(REPOSITORY, COMMIT)
                self.assertEqual(api.call_count, 1)

    def test_latest_ci_must_finish_successfully_even_with_older_success(self):
        for status, conclusion in [('in_progress', None), ('queued', None), ('completed', 'failure'),
                                   ('completed', 'cancelled'), ('completed', 'skipped')]:
            with self.subTest(status=status, conclusion=conclusion), patch.object(
                ci_evidence, 'api_pages', return_value=[{'workflow_runs': [
                    run(id=10), run(status=status, conclusion=conclusion),
                ]}],
            ) as api:
                with self.assertRaisesRegex(ValueError, '尚未完整通过'):
                    ci_evidence.select_evidence(REPOSITORY, COMMIT)
                self.assertEqual(api.call_count, 1)

    def test_invalid_repository_or_short_sha_never_reaches_github(self):
        for repository, commit in [('../repo', COMMIT), (REPOSITORY, 'abc123')]:
            with self.subTest(repository=repository, commit=commit), patch.object(ci_evidence, 'api_pages') as api:
                with self.assertRaises(ValueError):
                    ci_evidence.select_evidence(repository, commit)
                api.assert_not_called()

    def test_api_errors_do_not_expose_response_or_credentials(self):
        result = subprocess.CompletedProcess([], 1, 'SYNTHETIC_SECRET', 'SYNTHETIC_SECRET')
        with patch.object(ci_evidence.subprocess, 'run', return_value=result):
            with self.assertRaises(ValueError) as error:
                ci_evidence.api_pages('synthetic')
        self.assertNotIn('SYNTHETIC_SECRET', str(error.exception))

    def test_main_emits_action_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            selected = {'run_id': 20, 'head_sha': COMMIT, 'status': 'success'}
            with patch.object(ci_evidence, 'select_evidence', return_value=selected), patch.dict(
                os.environ, {'GITHUB_OUTPUT': str(output)},
            ), redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(ci_evidence.main([REPOSITORY, COMMIT]), 0)
            self.assertEqual(output.read_text(), f'run_id=20\nhead_sha={COMMIT}\nstatus=success\n')
            self.assertEqual(json.loads(stdout.getvalue()), selected)
