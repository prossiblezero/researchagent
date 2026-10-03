"""Failure and concurrency checks for temporary native sandbox permissions."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_agent.experiment_acl import scoped_denials
from research_agent.experiment_process import NativeCommand, ROOT, run_process


@unittest.skipUnless(os.name == 'nt', 'Windows native ACL lifecycle')
class ExperimentACLTests(unittest.TestCase):
    def test_concurrent_scope_waits_and_timeout_does_not_break_holder(self):
        with tempfile.TemporaryDirectory() as folder:
            entered = threading.Event()
            release = threading.Event()
            errors = []

            def holder():
                try:
                    with scoped_denials(folder, [], timeout=5, cancelled=lambda: False):
                        entered.set()
                        release.wait(5)
                except Exception as exc:
                    errors.append(exc)
                    entered.set()

            thread = threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(errors, [])
                with self.assertRaisesRegex(RuntimeError, 'awaiting isolation lock'):
                    with scoped_denials(folder, [], timeout=.15, cancelled=lambda: False):
                        self.fail('Concurrent sandbox acquired the active lock')
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            with scoped_denials(folder, [], timeout=1, cancelled=lambda: False):
                pass

    def test_unfinished_cleanup_blocks_a_new_process(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'data/native-acl'
            directory.mkdir(parents=True)
            journal = directory / 'interrupted.json'
            journal.write_text(json.dumps({'status': 'running', 'before': []}))
            with self.assertRaisesRegex(RuntimeError, 'Unfinished native ACL cleanup'):
                with scoped_denials(folder, [], timeout=1, cancelled=lambda: False):
                    self.fail('Unresolved cleanup allowed a new process')

    def test_cleanup_failure_keeps_process_receipt_and_cannot_pass(self):
        @contextmanager
        def failure(*args, **kwargs):
            yield 1
            raise OSError('cleanup failed')

        command = NativeCommand(['fixture'], ROOT / 'experiments/fixture', [])
        with patch('research_agent.experiment_acl.scoped_denials', failure), \
                patch('research_agent.experiment_process._run_process', return_value={
                    'exit_code': 0, 'termination': 'completed', 'seconds': .1,
                    'stdout': 'saved diagnostic', 'stderr': ''}):
            result = run_process(command, ROOT, timeout=1)
        self.assertEqual(result['termination'], 'isolation_cleanup_error')
        self.assertEqual(result['stdout'], 'saved diagnostic')
        self.assertEqual(result['seconds'], .1)
        self.assertIn('total_seconds', result)

    def test_waiting_for_isolation_does_not_change_measured_process_latency(self):
        @contextmanager
        def scope(*args, **kwargs):
            yield 5

        command = NativeCommand(['fixture'], ROOT / 'experiments/fixture', [])
        with patch('research_agent.experiment_acl.scoped_denials', scope), \
                patch('research_agent.experiment_process.time.monotonic', side_effect=[0, 21]), \
                patch('research_agent.experiment_process._run_process', return_value={
                    'exit_code': 0, 'termination': 'completed', 'seconds': 1,
                    'stdout': '[]', 'stderr': ''}):
            result = run_process(command, ROOT, timeout=30)
        self.assertEqual(result['seconds'], 1)
        self.assertEqual(result['total_seconds'], 21)

    def test_workspace_or_ancestor_cannot_be_a_denied_sibling(self):
        workspace = ROOT / 'experiments/fixture/job'
        for path in (workspace, workspace.parent, ROOT / 'experiments'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                NativeCommand(['fixture'], workspace, [path])
