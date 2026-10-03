"""Synthetic split selection through the real A-MEM preparation boundary."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

from evals import run_amem_acceptance as amem


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AMEMPreparationTests(unittest.TestCase):
    def fixture(self, directory, split='development'):
        root = Path(directory)
        case_dir = root / 'datasets/base'
        paper = root / 'paper'
        runtime = root / 'runtime'
        workspace = root / 'workspace'
        for folder in (case_dir, paper, workspace):
            folder.mkdir(parents=True)
        files = {
            'datasets/base/PROTOCOL.md': b'DEVELOPMENT PROTOCOL',
            'datasets/base/development-labels.json': b'[{"id":"q1","answer":"DEV_SECRET"}]',
            'datasets/held/PROTOCOL.md': b'HOLDOUT PROTOCOL',
            'datasets/held/labels.json': b'[{"id":"q1","answer":"HOLDOUT_SECRET"}]',
            'runtime/Scripts/python.exe': b'fixture only; never executed',
            'runtime/Lib/site-packages/research-base.pth': str(root/'.venv-v3/Lib/site-packages').encode(),
            'paper/2502.12110.pdf': b'fixture PDF; parsing mocked by saved pages',
            'paper/paper-pages.json': json.dumps({'title': 'Synthetic paper', 'source_url': 'https://example.org/paper',
                'page_count': 1, 'pages': [[1, 'Evidence in a synthetic document.']],
                'warnings': [], 'boundary': 'synthetic'}).encode(),
        }
        for name, raw in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        assets = {'upstream_files': {}, 'upstream_revision': 'synthetic', 'packages': [],
                  'paper_sha256': digest(files['paper/2502.12110.pdf']),
                  'paper_pages_sha256': digest(files['paper/paper-pages.json']),
                  'runtime_files': {name.removeprefix('runtime/'): digest(raw)
                                    for name, raw in files.items() if name.startswith('runtime/')}}
        asset_raw = json.dumps(assets).encode()
        (case_dir/'assets.json').write_bytes(asset_raw)
        case = {'audit_dataset': 'locomo', 'assets_sha256': digest(asset_raw), 'protocol_sha256': digest(files['datasets/base/PROTOCOL.md']),
                'labels_sha256': digest(files['datasets/base/development-labels.json'])}
        if split == 'holdout':
            case.update(split='holdout', protocol_source='datasets/held/PROTOCOL.md',
                        labels_source='datasets/held/labels.json',
                        protocol_sha256=digest(files['datasets/held/PROTOCOL.md']),
                        labels_sha256=digest(files['datasets/held/labels.json']))
        for name, value in [('tasks', [{'id': 'q1', 'conversation_id': 'synthetic-c', 'split': split}]),
                            ('corpus', [{'conversation_id': 'synthetic-c', 'split': split, 'conversation': {}}])]:
            target = workspace / f'data/locomo/{name}.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value), encoding='utf-8')
        app = SimpleNamespace(store=SimpleNamespace(path=root/'private/state.sqlite'),
                              library=SimpleNamespace(save=Mock(return_value=({'id': 'material'}, False))))
        return root, workspace, app, case

    def run_prepare(self, root, workspace, app, case):
        with patch.multiple(amem, ROOT=root, CASE=root/'datasets/base/case.json',
                            SOURCE=root/'upstream', PAPER=root/'paper', RUNTIME=root/'runtime'):
            return amem.prepare(app, {'id': 'job', 'space_id': 'space'}, workspace, case)

    def test_default_development_and_explicit_holdout_keep_labels_private(self):
        for split, secret, protocol in [('development', b'DEV_SECRET', b'DEVELOPMENT PROTOCOL'),
                                        ('holdout', b'HOLDOUT_SECRET', b'HOLDOUT PROTOCOL')]:
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                root, workspace, app, case = self.fixture(tmp, split)
                result = self.run_prepare(root, workspace, app, case)
                self.assertEqual(result['split'], split)
                self.assertEqual((workspace/'PROTOCOL.md').read_bytes(), protocol)
                self.assertIn(secret, (root/'private/auto-research/job/scoring-labels.json').read_bytes())
                self.assertEqual(app.library.save.call_count, 1)
                for file in workspace.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(b'DEV_SECRET', file.read_bytes())
                        self.assertNotIn(b'HOLDOUT_SECRET', file.read_bytes())

    def test_wrong_hash_or_unsafe_source_fails_before_preparation_side_effects(self):
        for field, value in [('labels_sha256', '0'*64), ('protocol_sha256', '0'*64),
                             ('labels_source', '../outside.json'), ('protocol_source', '../outside.md')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root, workspace, app, case = self.fixture(tmp)
                case[field] = value
                before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
                with self.assertRaises(ValueError):
                    self.run_prepare(root, workspace, app, case)
                app.library.save.assert_not_called()
                self.assertFalse((root/'private').exists())
                self.assertEqual(before, {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()})

    def test_split_mismatch_empty_or_malformed_data_fails_before_side_effects(self):
        for name, value in [('tasks', []), ('tasks', [{'split': 'holdout'}]),
                            ('corpus', [{'split': 'holdout'}]), ('tasks', [{}]),
                            ('tasks', ['not a row']), ('tasks', [{'split': []}])]:
            with self.subTest(name=name, value=value), tempfile.TemporaryDirectory() as tmp:
                root, workspace, app, case = self.fixture(tmp)
                (workspace/f'data/locomo/{name}.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.run_prepare(root, workspace, app, case)
                app.library.save.assert_not_called()
                self.assertFalse((root/'private').exists())

    def test_selected_files_are_in_the_private_host_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, workspace, app, case = self.fixture(tmp, 'holdout')
            selected_case = root/'datasets/held/case.json'
            selected_case.write_text(json.dumps({'campaign': case}), encoding='utf-8')
            output = root/'report'
            with patch.multiple(amem, ROOT=root, CASE=root/'datasets/base/case.json'), \
                    patch.object(amem, 'campaign', return_value=True) as campaign, \
                    patch('sys.argv', ['run_amem_acceptance.py', '--output', str(output),
                                       '--case-file', str(selected_case)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(amem.main(), 0)
            campaign.assert_called_once()
            condition = json.loads((output/'condition.json').read_text(encoding='utf-8'))
            with zipfile.ZipFile(output/'evaluated-source-and-cases.zip') as archive:
                for field in ('protocol', 'labels'):
                    name = case[field + '_source']
                    self.assertEqual(archive.read(name), (root/name).read_bytes())
                    self.assertEqual(condition['source_sha256'][name], case[field + '_sha256'])

    def test_labels_require_a_source_under_denied_datasets_and_locomo_contract(self):
        for condition in ('readable_root_source', 'no_locomo_contract'):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as tmp:
                root, workspace, app, case = self.fixture(tmp)
                if condition == 'readable_root_source':
                    (root/'readable-labels.json').write_bytes((root/'datasets/base/development-labels.json').read_bytes())
                    case['labels_source'] = 'readable-labels.json'
                else:
                    case.pop('audit_dataset')
                with self.assertRaises(ValueError):
                    self.run_prepare(root, workspace, app, case)
                app.library.save.assert_not_called()
                self.assertFalse((root/'private').exists())

    def test_invalid_split_is_rejected_before_side_effects(self):
        for split in ['test', '', None, [], {}]:
            with self.subTest(split=split), tempfile.TemporaryDirectory() as tmp:
                root, workspace, app, case = self.fixture(tmp)
                case['split'] = split
                with self.assertRaises(ValueError):
                    self.run_prepare(root, workspace, app, case)
                app.library.save.assert_not_called()
                self.assertFalse((root/'private').exists())


if __name__ == '__main__':
    unittest.main()
