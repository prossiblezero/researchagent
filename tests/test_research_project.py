"""Public project staging must preserve prior work and isolate dependency execution."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from research_agent.research_project import prepare, unpack, python_for
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore
from research_agent.workbench_store import Conflict


class ResearchProjectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = WorkbenchStore(self.root/'db.sqlite')
        space = self.store.save_space({'name': 'Project'})['id']
        chat = self.store.create_conversation(space, 'Goal')['id']
        model = Mock(); model.name = 'test'
        self.app = Workbench(self.store, lambda: model, Mock(), self.root/'traces', start_worker=False)
        self.app.coding.work_root = self.root/'work'
        self.app.auto_research.enqueue(space, chat, 'Prepare a public baseline')
        self.job = self.store.claim_next()
        self.workspace = self.app.coding.work_root/self.job['id']
        self.addCleanup(self.app.close)
        download = patch('research_agent.research_project.DOWNLOAD_ROOT', self.root/'paper')
        download.start(); self.addCleanup(download.stop)

    def test_download_stages_real_bytes_and_reuses_receipt_without_network(self):
        request = {'sources': [{'url': 'https://example.org/data.json', 'kind': 'file', 'destination': 'data/input.json'}], 'packages': []}
        with patch('research_agent.research_project.fetch_public', return_value=(b'{"task":1}', 'application/json', request['sources'][0]['url'], 'utf-8')) as fetch:
            first = prepare(self.app, self.job, request, 'download-one')
            second = prepare(self.app, self.job, request, 'download-one')
        self.assertTrue(first['ok'], first)
        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((self.workspace/'data/input.json').read_bytes(), b'{"task":1}')
        self.assertTrue(Path(first['sources'][0]['download_path']).is_relative_to(self.root/'paper'))
        (self.workspace/'data/input.json').write_text('user modified', encoding='utf-8')
        failed = prepare(self.app, self.job, request, 'download-two')
        self.assertFalse(failed['ok'])
        self.assertEqual((self.workspace/'data/input.json').read_text(), 'user modified')

    def test_frozen_locomo_case_cannot_download_a_second_copy_of_labels(self):
        payload = json.loads(self.job['payload'])
        payload['auto_research']['metric_contract'] = {'name': 'locomo_qa_v1'}
        self.job['payload'] = json.dumps(payload)
        with patch('research_agent.research_project.fetch_public') as fetch, self.assertRaises(Conflict):
            prepare(self.app, self.job, {'sources': [{'url': 'https://example.org/locomo10.json',
                'kind': 'file', 'destination': 'extra/raw.json'}], 'packages': []}, 'labels-download')
        fetch.assert_not_called()

    def archive(self, items):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for name, content in items:
                archive.writestr(name, content)
        return data.getvalue()

    def test_archive_preserves_source_root_and_rejects_traversal_before_writing(self):
        good = self.archive([('repo-sha/main.py', 'print(1)'), ('repo-sha/LICENSE', 'public'), ('repo-sha/.env', 'not imported')])
        files = unpack(good, self.workspace, 'baseline')
        self.assertEqual(set(files), {'baseline/main.py', 'baseline/LICENSE'})
        self.assertFalse((self.workspace/'baseline/.env').exists())
        for name in ('../outside.py', '/absolute.py', 'C:/outside.py', 'repo/../outside.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                unpack(self.archive([(name, 'bad')]), self.workspace, 'unsafe')
        self.assertFalse((self.workspace/'unsafe').exists())

    def test_dependency_commands_use_pinned_wheels_and_sandboxed_install(self):
        commands = []
        def create(builder, root):
            (root/'Scripts').mkdir(parents=True)
            (root/'Scripts/python.exe').write_bytes(b'python interpreter fixture')
        def runner(args, cwd, **kwargs):
            commands.append(args)
            return {'exit_code': 0, 'termination': 'completed', 'seconds': .1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.research_project.venv.EnvBuilder.create', create), \
             patch('research_agent.research_project.sandbox_command', side_effect=lambda _, args, *rest: ['sandbox', *args]), \
             patch('research_agent.research_project.run_process', side_effect=runner):
            result = prepare(self.app, self.job, {'sources': [], 'packages': ['numpy==2.2.6']}, 'packages')
        self.assertTrue(result['ok'], result)
        self.assertEqual(commands[0][0], 'sandbox')
        self.assertEqual(commands[2][0], 'sandbox')
        self.assertIn('--only-binary=:all:', commands[1])
        self.assertIn('--no-index', commands[2])
        self.assertEqual(python_for(self.app, self.job['id']), str(self.workspace/'.venv/Scripts/python.exe'))
        (self.workspace/'.venv/Scripts/python.exe').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            python_for(self.app, self.job['id'])

    def test_rejects_unpinned_or_executable_dependency_specifications(self):
        for package in ('numpy', 'https://example.org/evil.whl', '-e .', 'numpy==1; Invoke-Expression bad'):
            with self.subTest(package=package), self.assertRaises(ValueError):
                prepare(self.app, self.job, {'sources': [], 'packages': [package]}, 'invalid')
        with self.assertRaises(ValueError):
            prepare(self.app, self.job, {'sources': [{'url': 'https://example.org/file', 'kind': 'file', 'destination': '.venv/Scripts/python.exe'}], 'packages': []}, 'invalid')


if __name__ == '__main__':
    unittest.main()
