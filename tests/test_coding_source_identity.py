"""Actual SQLite persistence must not confuse redacted examples with source changes."""
import hashlib
import json
from unittest.mock import patch
import unittest
from contextlib import closing

import tests.test_coding_tool as fixtures
from research_agent.workbench_store import Conflict


class SourceIdentityTests(unittest.TestCase):
    setUp = fixtures.CodingToolTests.setUp

    def test_resume_uses_original_source_identity_after_redaction(self):
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        code = "api_key = 'public-example-only'\nprint('example')\n"
        path = workspace / 'evaluate.py'
        path.write_bytes(code.encode())
        task = self.tool.submit(self.job, 'saved-source', self.request)
        snapshot = self.tool.source_snapshot(workspace)
        hashes = {'evaluate.py': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.tool.save(task['id'], {'coded': True, 'after': snapshot,
            'readonly_source_sha256': hashes}, 'interrupted')
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET resume_checkpoint_id=1 WHERE id=?', (self.job['id'],))
        stored = self.tool.get(self.space, task['id'])['state']
        self.assertNotEqual(stored['after'], snapshot)
        self.assertEqual(stored['readonly_source_sha256'], hashes)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashes['evaluate.py'])
        resumed = self.tool.resume(self.space, task['id'])
        self.assertEqual(resumed['status'], 'queued')


    def test_actual_source_changes_and_unknown_execution_still_reject_resume(self):
        workspace = self.tool.work_root / self.job['id']; workspace.mkdir(parents=True)
        code = "api_key = 'public-example-only'\n"
        path = workspace / 'evaluate.py'; path.write_bytes(code.encode())
        task = self.tool.submit(self.job, 'changed-source', self.request)
        snapshot = self.tool.source_snapshot(workspace)
        hashes = {'evaluate.py': hashlib.sha256(path.read_bytes()).hexdigest()}
        state = {'coded': True, 'after': snapshot, 'readonly_source_sha256': hashes}
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET resume_checkpoint_id=1 WHERE id=?', (self.job['id'],))
        for failure in ('same_redaction', 'line_endings', 'missing_hash', 'no_hash', 'unknown_execution'):
            with self.subTest(failure=failure):
                path.write_bytes(code.encode())
                changed = dict(state)
                if failure == 'same_redaction': path.write_bytes(code.replace('public-example-only', 'different-public-example').encode())
                if failure == 'line_endings': path.write_bytes(code.replace('\n', '\r\n').encode())
                if failure == 'missing_hash': changed['readonly_source_sha256'] = {}
                if failure == 'no_hash': changed.pop('readonly_source_sha256')
                if failure == 'unknown_execution': changed['measurement_started'] = 0
                self.tool.save(task['id'], changed, 'interrupted')
                with self.assertRaises(Conflict):
                    self.tool.resume(self.space, task['id'])

    def test_execution_only_reentry_preserves_measurement_without_replay(self):
        workspace = self.tool.work_root / self.job['id']; workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_bytes(b"api_key = 'public-example-only'\n")
        request = {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0}
        task = self.tool.submit(self.job, 'execution-only', request)
        self.tool.save(task['id'], task['state'], 'queued')  # Persist/redact before first launch.
        def runner(*args, **kwargs):
            (workspace / 'result.json').write_text(json.dumps({'metrics': {'score': .7}}))
            return {'exit_code': 0, 'termination': 'completed', 'seconds': 1,
                    'tool_calls': 1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.coding_tool.sandbox_command', return_value=['fixture']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner) as run:
            self.tool.execute(self.space, task['id'])
            result = self.tool.get(self.space, task['id'])
            self.assertEqual(result['status'], 'completed', result['state'].get('error'))
            self.tool.execute(self.space, task['id'])
            self.assertEqual(self.tool.get(self.space, task['id'])['status'], 'completed')
            # Historical tasks already measured have original readonly hashes.
            saved = self.tool.get(self.space, task['id'])['state']
            saved.pop('evaluation_source_sha256', None)
            self.tool.save(task['id'], saved, 'queued')
            self.tool.execute(self.space, task['id'])
            result = self.tool.get(self.space, task['id'])
            self.assertEqual(result['status'], 'completed', result['state'].get('error'))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result['state']['measurements'][0]['metrics']['score'], .7)

    def test_legacy_redacted_execution_without_hashes_fails_closed(self):
        workspace = self.tool.work_root / self.job['id']; workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_bytes(b"api_key = 'public-example-only'\n")
        task = self.tool.submit(self.job, 'legacy', {**self.request, 'execution_only': True,
                                                   'seconds': 0, 'token_budget': 0})
        state = task['state']; state.pop('evaluation_source_sha256', None)
        self.tool.save(task['id'], state, 'queued')
        with patch('research_agent.coding_tool.run_process') as run:
            self.tool.execute(self.space, task['id'])
            self.assertEqual(self.tool.get(self.space, task['id'])['status'], 'failed')
            run.assert_not_called()


    def test_freezing_cannot_replace_original_hash_with_racing_source_change(self):
        workspace = self.tool.work_root / self.job['id']; workspace.mkdir(parents=True)
        script = workspace / 'evaluate.py'
        script.write_bytes(b"api_key = 'public-example-only'\n")
        task = self.tool.submit(self.job, 'freeze-race', {**self.request, 'execution_only': True,
                                                        'seconds': 0, 'token_budget': 0})
        def changed_during_materialization(_workspace, names):
            if 'evaluate.py' in names:
                script.write_bytes(b"api_key = 'different-public-example'\n")
        with patch('research_agent.coding_tool.materialize_readonly_files', side_effect=changed_during_materialization), \
             patch('research_agent.coding_tool.run_process') as run:
            self.tool.execute(self.space, task['id'])
            run.assert_not_called()
        saved = self.tool.get(self.space, task['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['state']['readonly_source_sha256'], task['state']['evaluation_source_sha256'])


if __name__ == '__main__':
    unittest.main()
