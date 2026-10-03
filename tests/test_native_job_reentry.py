"""Opt-in native regression for alternating frozen research workspaces."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
import subprocess
from types import SimpleNamespace
import unittest
from uuid import uuid4

from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt' and os.environ.get('RESEARCH_RUN_NATIVE_TESTS') == '1',
                     'native sandbox opt-in')
class NativeJobReentryTests(unittest.TestCase):
    def assert_same_access(self, before, after):
        def comparable(entry, permit_redundant):
            sddl = entry['sddl']
            # A redundant grant disappeared after return; its writer is unknown.
            # Normalize only the observed same-group grant without any Deny ACE.
            ace = '(A;OICI;0x1301bf;;;' + entry['sandbox_sid'] + ')'
            if permit_redundant:
                if ace.replace('(A;OICI;', '(A;OICIID;', 1) in sddl:
                    sddl = sddl.replace(ace, '')
            return entry['path'], entry['owner'], entry['keys'], sddl
        self.assertEqual(len(before), len(after))
        for left, right in zip(before, after):
            permit = '(D;' not in left['sddl'] and '(D;' not in right['sddl']
            self.assertEqual(comparable(left, permit), comparable(right, permit))

    def test_previous_workspace_allow_grants_survive_temporary_denial(self):
        from research_agent.experiment_acl import acl_snapshot
        from research_agent.experiment_process import sandbox_command, run_process
        root = ROOT / 'experiments' / ('native-prior-grant-' + uuid4().hex)
        sibling = root / 'previous'
        active = root / 'active'
        sibling.mkdir(parents=True)
        active.mkdir()
        (sibling / 'marker.txt').write_text('private')
        first = run_process(sandbox_command(sibling, [sys.executable, '-c', 'print("prepared")']),
                            sibling, timeout=30)
        self.assertEqual(first['termination'], 'completed', first)
        # Reproduce legacy workspace ACLs: recent Codex only adds capability
        # grants, while older workspaces also have this explicit group grant.
        setup = r'''
$ErrorActionPreference = 'Stop'
$path = [Console]::In.ReadToEnd()
$acl = Get-Acl -LiteralPath $path
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    'CodexSandboxUsers', [System.Security.AccessControl.FileSystemRights]1245631,
    [System.Security.AccessControl.InheritanceFlags]3,
    [System.Security.AccessControl.PropagationFlags]0,
    [System.Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl
'''
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', setup],
                       input=str(sibling), text=True, check=True, capture_output=True,
                       env={k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'},
                       creationflags=0x08000000, timeout=30)
        before = acl_snapshot('snapshot', [{'path': str(sibling)}])
        self.assertTrue(before[0]['allows'], before)
        code = (f'from pathlib import Path\ntry: Path({str(sibling / "marker.txt")!r}).read_bytes()\n'
                'except PermissionError: pass\nelse: raise AssertionError("sibling readable")\n')
        second = run_process(sandbox_command(active, [sys.executable, '-c', code], [sibling]),
                             active, timeout=30)
        after = acl_snapshot('snapshot', [{'path': str(sibling)}])
        record = ROOT / 'evals/reports/sandbox-auto-acl-recovery-20261001' / (root.name + '.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'before': before, 'after': after, 'first': first, 'second': second},
                                     indent=2), encoding='utf-8')
        self.assertEqual(second['termination'], 'completed', second)
        self.assert_same_access(before, after)
        # Independently exercise restoration when the grant is already missing
        # at cleanup time, rather than only the service's later normalization.
        remove_fixture_grant = r'''
$ErrorActionPreference = 'Stop'
$path = [Console]::In.ReadToEnd()
$acl = Get-Acl -LiteralPath $path
$sid = ([System.Security.Principal.NTAccount]::new('CodexSandboxUsers')).Translate([System.Security.Principal.SecurityIdentifier]).Value
$ace = '(A;OICI;0x1301bf;;;' + $sid + ')'
if ($acl.Sddl.Contains($ace)) {
    $acl.SetSecurityDescriptorSddlForm($acl.Sddl.Replace($ace,''), [System.Security.AccessControl.AccessControlSections]::Access)
    Set-Acl -LiteralPath $path -AclObject $acl
}
'''
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', remove_fixture_grant],
                       input=str(sibling), text=True, check=True, capture_output=True,
                       env={k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'},
                       creationflags=0x08000000, timeout=30)
        missing = acl_snapshot('snapshot', [{'path': str(sibling)}])
        self.assertFalse(missing[0]['allows'])
        restored = acl_snapshot('restore', before)
        self.assertEqual(restored[0]['restored'], before[0]['allows'])
        self.assertEqual(restored[0]['sddl'], before[0]['sddl'])

    def test_timeout_removes_only_new_sibling_denials(self):
        from research_agent.experiment_acl import acl_snapshot
        from research_agent.experiment_process import sandbox_command, run_process
        root = ROOT / 'experiments' / ('native-timeout-' + uuid4().hex)
        workspace = root / 'active'
        sibling = root / 'sibling'
        workspace.mkdir(parents=True)
        sibling.mkdir()
        (sibling / 'marker.txt').write_text('private')
        before = acl_snapshot('snapshot', [{'path': str(sibling)}])
        command = sandbox_command(workspace, [sys.executable, '-c',
                                   'import time; print("started",flush=True); time.sleep(30)'], [sibling])
        result = run_process(command, workspace, timeout=8)
        after = acl_snapshot('snapshot', [{'path': str(sibling)}])
        record = ROOT / 'evals/reports/sandbox-acl-recovery-20260930' / (root.name + '.json')
        record.write_text(json.dumps({'before': before, 'after': after, 'result': result}, indent=2), encoding='utf-8')
        self.assertEqual(result['termination'], 'timeout', result)
        self.assertIn('started', result['stdout'])
        self.assertEqual(before, after)

    def test_alternating_jobs_keep_own_outputs_readable_and_sibling_private(self):
        self.check_alternating_jobs()

    def test_formal_auto_directory_supports_alternating_jobs(self):
        self.check_alternating_jobs(ROOT / 'experiments/auto')

    def check_alternating_jobs(self, work_root=None):
        from research_agent.experiment_acl import acl_snapshot
        with tempfile.TemporaryDirectory(prefix='native-reentry-') as temporary:
            private = Path(temporary)
            store = WorkbenchStore(private / 'state.sqlite')
            app = Workbench(store, lambda: SimpleNamespace(name='offline-fixture'), None,
                            private / 'traces', start_worker=False)
            app.coding.work_root = work_root or ROOT / 'experiments' / ('native-reentry-' + uuid4().hex)
            record = ROOT / 'evals/reports/sandbox-auto-acl-recovery-20261001/reentry' / uuid4().hex
            record.mkdir(parents=True)
            try:
                values = {
                    'tasks': [{'id': 'q', 'conversation_id': 'c', 'split': 'development', 'category': 1}],
                    'corpus': [{'conversation_id': 'c', 'split': 'development',
                                'conversation': {'session_1': [{'dia_id': 'D1:1'}]}}],
                    'labels': [{'id': 'q', 'answer': 'secret', 'gold_evidence': ['D1:1'],
                                'retrieval_scorable': True}],
                }
                raw = {name: json.dumps(value).encode() for name, value in values.items()}
                contract = {'name': 'locomo_qa_v1', 'seeds': [13], 'tasks_path': 'tasks.json',
                            'corpus_path': 'corpus.json',
                            **{name + '_sha256': hashlib.sha256(value).hexdigest()
                               for name, value in raw.items()}}
                space = store.save_space({'name': 'native alternating jobs'})['id']
                jobs = []
                for name in ('A', 'B'):
                    chat = store.create_conversation(space, name)['id']
                    app.auto_research.enqueue(space, chat, 'Native isolation fixture',
                                              metric_contract=contract,
                                              budget={'coding_calls': 0, 'experiment_seconds': 90})
                    job = store.claim_next()
                    workspace = app.coding.work_root / job['id']
                    workspace.mkdir(parents=True)
                    labels = private / 'auto-research' / job['id']
                    labels.mkdir(parents=True)
                    (labels / 'scoring-labels.json').write_bytes(raw['labels'])
                    for kind in ('tasks', 'corpus'):
                        (workspace / (kind + '.json')).write_bytes(raw[kind])
                    (workspace / 'private-marker.txt').write_text(name)
                    jobs.append(job)
                for index, job in enumerate(jobs):
                    workspace = app.coding.work_root / job['id']
                    sibling = app.coding.work_root / jobs[1 - index]['id'] / 'private-marker.txt'
                    script = (
                        'import json,sys\nfrom pathlib import Path\n'
                        f'try: Path({str(sibling)!r}).read_bytes()\n'
                        'except PermissionError: pass\n'
                        'else: raise AssertionError("sibling is readable")\n'
                        'assert Path("private-marker.txt").read_text() in ("A", "B")\n'
                        'Path(sys.argv[1]+".jsonl").write_text(json.dumps({"task_id":"q",'
                        '"seed":13,"answer":"wrong","evidence_ids":["D1:1"]}))\n'
                        'Path(sys.argv[1]).write_text(json.dumps({"metrics":{"answer_f1":1},'
                        '"config":{"dataset":"locomo","dataset_version":'
                        f'{contract["tasks_sha256"]!r},"split":"development","seeds":[13]}},'
                        '"diagnostics":{"predictions_path":sys.argv[1]+".jsonl"}}))\n'
                    )
                    (workspace / 'evaluate.py').write_text(script, encoding='utf-8')
                for round_number, index in enumerate((0, 1, 0)):
                    job = jobs[index]
                    transient = [path for path in app.coding.private_paths(job['id'])
                                 if path.is_relative_to(ROOT / 'experiments')]
                    before_acl = acl_snapshot('snapshot', [{'path': str(p)} for p in transient])
                    result_name = f'result-{round_number}.json'
                    task = app.coding.submit(job, f'round-{round_number}', {
                        'task': 'Check alternating workspace access', 'plan': 'Offline native fixture',
                        'execution_only': True, 'seconds': 0, 'token_budget': 0,
                        'commands': [{'name': f'round-{round_number}', 'role': 'diagnostic',
                                      'script': 'evaluate.py', 'args': [result_name],
                                      'result_path': result_name, 'seconds': 30}],
                    })
                    app.coding.execute(space, task['id'])
                    task = app.coding.get(space, task['id'])
                    (record / f'round-{round_number}.json').write_text(
                        json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
                    self.assertEqual(task['status'], 'completed', task['state'].get('error'))
                    measured = task['state']['measurements'][0]
                    self.assertTrue(measured['valid'], measured)
                    self.assertEqual(measured['metrics']['answer_f1'], 0)
                    self.assertTrue((app.coding.work_root / job['id'] / result_name).is_file())
                    self.assert_same_access(before_acl, acl_snapshot('snapshot', [{'path': str(p)} for p in transient]))
            finally:
                app.close()
