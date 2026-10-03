"""Real bounded Windows pipes; no model service or generated code execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from research_agent.experiment_process import run_process, clean_environment


@unittest.skipUnless(os.name == 'nt', 'Windows process supervisor')
class ProcessInteractionTests(unittest.TestCase):
    def test_dependency_dotenv_discovery_is_disabled(self):
        self.assertEqual(clean_environment()['PYTHON_DOTENV_DISABLED'], '1')

    def run_script(self, code, handler, timeout=10, cancelled=lambda: False):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / 'experiment.py'
            script.write_text(code, encoding='utf-8')
            return run_process([sys.executable, '-u', str(script)], folder,
                               timeout=timeout, model_request=handler, cancelled=cancelled)

    def test_model_wait_does_not_suspend_process_timeout_or_cancellation(self):
        for reason in ('timeout', 'cancelled'):
            with self.subTest(reason=reason):
                stop = threading.Event()
                calls = []
                def slow_model(*args):
                    calls.append(True)
                    if reason == 'cancelled':
                        stop.set()
                    time.sleep(1.5)
                    return {'id': 'r', 'content': 'late response'}
                result = self.run_script(
                    'import time\nprint(\'RESEARCH_MODEL_REQUEST {"id":"r"}\',flush=True)\n'
                    'time.sleep(1)\nprint("executed after stop",flush=True)\ntime.sleep(30)\n',
                    slow_model, timeout=.7 if reason == 'timeout' else 10, cancelled=stop.is_set)
                self.assertEqual(calls, [True])
                self.assertEqual(result['termination'], reason, result)
                self.assertNotIn('executed after stop', result['stdout'])

    def test_second_request_uses_first_response_and_keeps_logs(self):
        calls = []
        def model(request, deadline):
            self.assertGreater(deadline, time.monotonic())
            calls.append(request)
            return {'id': request['id'], 'content': 'first answer' if len(calls) == 1 else 'final answer'}
        result = self.run_script(
            'import json,sys\n'
            'print("ordinary log",flush=True)\n'
            'for i in range(2):\n'
            ' text="initial" if i==0 else response["content"]\n'
            ' print("RESEARCH_MODEL_REQUEST "+json.dumps({"id":str(i),"messages":[{"role":"user","content":text}]}),flush=True)\n'
            ' response=json.loads(sys.stdin.readline())\n'
            'print(response["content"],flush=True)\n', model)
        self.assertEqual(result['termination'], 'completed', result)
        self.assertEqual(calls[1]['messages'][0]['content'], 'first answer')
        self.assertIn('ordinary log', result['stdout'])
        self.assertIn('final answer', result['stdout'])
        self.assertNotIn('"messages"', result['stdout'])

    def test_protocol_and_host_failures_stop_without_hanging(self):
        for line, handler in [
            ('RESEARCH_MODEL_REQUEST not-json', lambda *a: self.fail('must not call host')),
            ('RESEARCH_MODEL_REQUEST '+ '['*2000+'0'+']'*2000, lambda *a: self.fail('must not call host')),
            ('RESEARCH_MODEL_REQUEST null', lambda *a: self.fail('must not call host')),
            ('RESEARCH_MODEL_REQUEST {}', lambda *a: (_ for _ in ()).throw(ValueError('invalid request'))),
        ]:
            with self.subTest(line=line):
                result = self.run_script(f'import time\nprint({line!r},flush=True)\ntime.sleep(30)\n', handler)
                self.assertEqual(result['termination'], 'model_protocol_error', result)
                self.assertTrue(result['model_error'])
                self.assertLess(result['seconds'], 5)

    def test_exit_with_unanswered_request_is_not_success(self):
        result = self.run_script(
            'print(\'RESEARCH_MODEL_REQUEST {"id":"r"}\',flush=True)\n',
            lambda *args: {'id': 'r', 'content': 'unused'})
        self.assertEqual(result['termination'], 'model_protocol_error', result)

    def test_supervisor_failure_stops_the_process(self):
        def unavailable():
            raise RuntimeError('state unavailable')
        result = self.run_script('import time\ntime.sleep(.4)\nprint("unsafe continuation")\n',
                                 lambda *args: {}, timeout=.1, cancelled=unavailable)
        self.assertEqual(result['termination'], 'supervision_error', result)
        self.assertNotIn('unsafe continuation', result['stdout'])

    def test_unread_large_reply_cannot_block_timeout(self):
        result = self.run_script(
            'import time\nprint(\'RESEARCH_MODEL_REQUEST {"id":"r"}\',flush=True)\ntime.sleep(30)\n',
            lambda *a: {'id': 'r', 'content': 'x' * 64000}, timeout=1)
        self.assertEqual(result['termination'], 'timeout', result)
        self.assertLess(result['seconds'], 5)
