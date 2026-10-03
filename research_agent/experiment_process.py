"""Bounded native processes. Codex owns Windows filesystem/network enforcement."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .trace import redact

ROOT = Path(__file__).resolve().parents[1]


def toml(value):
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value)


def clean_environment():
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
               'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH'}
    return {**{k: v for k, v in os.environ.items() if k.upper() in allowed},
            'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHON_DOTENV_DISABLED': '1', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}


def codex_path():
    executable = shutil.which('codex')
    if os.name != 'nt' or not executable or Path(executable).suffix.lower() != '.exe':
        raise RuntimeError('V4 首版需要 Windows 原生 Codex CLI 及已配置的 elevated 沙箱')
    return str(Path(executable).resolve())


def permissions(workspace, private_paths=(), readonly_paths=()):
    # Native elevated sandbox currently requires root read. Never claim a VM/read allowlist.
    fs = {':root': 'read', str(Path(workspace).resolve()): 'write'}
    private = [ROOT / 'data', ROOT / 'evals', ROOT / 'docs', ROOT / '.git', ROOT / '.env',
               Path(os.environ.get('USERPROFILE', str(Path.home()))) / '.codex' / 'auth.json',
               Path(os.environ.get('USERPROFILE', str(Path.home()))) / '.ssh', *private_paths]
    for path in private:
        fs[str(Path(path).resolve())] = 'deny'
    fs[str(ROOT / '.env.*')] = 'deny'
    fs[str(Path(workspace).resolve()/'.git')] = 'read'
    for path in readonly_paths:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(Path(workspace).resolve()):
            raise ValueError('只读实验输入必须位于项目内')
        fs[str(resolved)] = 'read'
    return {'filesystem': fs, 'network': {'enabled': False}}


def sandbox_command(workspace, command, private_paths=(), readonly_paths=()):
    return NativeCommand([codex_path(), 'sandbox', '-C', str(workspace), '-P', 'research_v4',
            '-c', 'permissions.research_v4=' + toml(permissions(workspace, private_paths, readonly_paths)),
            '-c', 'windows.sandbox="elevated"', '--', *map(str, command)], workspace, private_paths)


class NativeCommand(list):
    def __init__(self, args, workspace, private_paths):
        super().__init__(args)
        workspace = Path(workspace).resolve()
        self.transient_denials = []
        for path in private_paths:
            path = Path(path).absolute()
            if path.is_relative_to(ROOT / 'experiments'):
                if workspace.is_relative_to(path.resolve()):
                    raise ValueError('Cannot deny an ancestor of the active experiment')
                self.transient_denials.append(path)


def coding_command(workspace, private_paths=(), token_budget=60000, *, readonly_paths=()):
    url = os.environ.get('SUDOCODE_BASE_URL', 'https://api.sudocode.chat/v1').rstrip('/')
    if not url.startswith('https://'):
        raise ValueError('Codex 模型服务须使用 HTTPS')
    config = {
        'model_provider': 'research_v4',
        'model_providers.research_v4': {'name': 'Configured research provider', 'base_url': url,
                                     'env_key': 'RESEARCH_V4_MODEL_KEY', 'wire_api': 'responses',
                                     'request_max_retries': 0, 'stream_max_retries': 0},
        'default_permissions': 'research_v4',
        'permissions.research_v4': permissions(workspace, private_paths, readonly_paths),
        'approval_policy': 'never', 'windows.sandbox': 'elevated',
        'shell_environment_policy.inherit': 'core', 'shell_environment_policy.ignore_default_excludes': False,
        'shell_environment_policy.filters': {'*KEY*': 'exclude', '*TOKEN*': 'exclude', '*SECRET*': 'exclude'},
        'shell_environment_policy.set': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'PYTHON_DOTENV_DISABLED': '1', 'GIT_OPTIONAL_LOCKS': '0'},
        'allow_login_shell': False, 'project_doc_max_bytes': 0,
        'skills.max_context_tokens': 1000,
        'features.apps': False, 'features.remote_plugin': False, 'features.hooks': False,
        'features.memories': False, 'features.multi_agent': False, 'agents.enabled': False,
        'features.shell_snapshot': False, 'web_search': 'disabled',
        'model_reasoning_effort': 'medium',
        'features.rollout_budget.enabled': True, 'features.rollout_budget.limit_tokens': token_budget,
        'features.rollout_budget.reminder_at_remaining_tokens': [token_budget // 2, token_budget // 10],
    }
    args = [codex_path(), 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
            '--json', '--color', 'never', '-C', str(workspace), '-m', 'gpt-5.6-luna']
    for key, value in config.items():
        args += ['-c', key + '=' + toml(value)]
    return NativeCommand(args + ['-'], workspace, private_paths)


def codex_events(stdout):
    """Keep malformed stream fragments as diagnostics, never lose a process receipt."""
    events = []
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError('event must be an object')
            events.append(redact(item, limit=None))
        except ValueError as exc:
            events.append({'type': 'invalid_jsonl', 'line': number, 'error': str(exc)})
    return events


def _windows_job(process):
    """Assign suspended process to a kill-on-close job before it can spawn children."""
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                    ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_ulonglong * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended(); info.basic.flags = 0x2000 | 0x200 | 0x8  # kill-on-close, job memory, active processes
    # PyTorch + local MiniLM exceeds the former 2 GiB commit limit on Windows.
    info.job_memory = 4 * 1024 * 1024 * 1024
    info.basic.active = 32
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        if handle: kernel.CloseHandle(handle)
        process.kill()
        raise OSError(ctypes.get_last_error(), '无法绑定 Windows 进程树，拒绝无约束运行')
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtResumeProcess.argtypes = [w.HANDLE]
    if ntdll.NtResumeProcess(int(process._handle)) != 0:
        kernel.CloseHandle(handle)
        raise RuntimeError('无法启动受控进程')
    close_lock = threading.Lock()
    def close():
        nonlocal handle
        with close_lock:
            if handle:
                kernel.CloseHandle(handle)
                handle = None
    return close


class ProcessNotStarted(RuntimeError):
    """Native isolation rejected the call before process creation was attempted."""


def run_process(args, workspace, *, timeout, cancelled=lambda: False, stdin='', env=None,
                event=None, tool_limit=20, output_limit=4 * 1024 * 1024, model_request=None):
    if isinstance(args, NativeCommand):
        from .experiment_acl import scoped_denials
        started = time.monotonic()
        result = None
        attempted = False
        try:
            with scoped_denials(ROOT, args.transient_denials, timeout=timeout, cancelled=cancelled) as remaining:
                if remaining <= 0 or cancelled():
                    raise RuntimeError('Native sandbox cancelled or timed out before process start')
                attempted = True
                result = _run_process(args, workspace, timeout=remaining, cancelled=cancelled, stdin=stdin,
                                      env=env, event=event, tool_limit=tool_limit, output_limit=output_limit,
                                      model_request=model_request)
        except Exception as exc:
            if not attempted:
                raise ProcessNotStarted(str(exc)) from exc
            if result is None:
                raise
            result.update(termination='isolation_cleanup_error', isolation_error=str(redact(str(exc))))
        result['total_seconds'] = round(time.monotonic() - started, 3)
        return result
    return _run_process(args, workspace, timeout=timeout, cancelled=cancelled, stdin=stdin,
                        env=env, event=event, tool_limit=tool_limit, output_limit=output_limit,
                        model_request=model_request)


def _run_process(args, workspace, *, timeout, cancelled=lambda: False, stdin='', env=None,
                 event=None, tool_limit=20, output_limit=4 * 1024 * 1024, model_request=None):
    """No shell interpolation; bounded output; process tree dies on timeout/cancel/crash."""
    if os.name != 'nt':
        raise RuntimeError('此执行适配器仅在 Windows 原生沙箱上启用')
    started = time.monotonic()
    process = subprocess.Popen(args, cwd=workspace, env=env or clean_environment(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=0x08000000 | 0x00000004)  # NO_WINDOW | SUSPENDED
    close_job = _windows_job(process)
    buffers = [bytearray(), bytearray()]; exceeded = threading.Event(); tools_seen = set()
    requests = queue.Queue(maxsize=1)
    replies = queue.Queue(maxsize=1)
    finished = threading.Event()
    protocol_errors = []
    prefix = b'RESEARCH_MODEL_REQUEST '
    request_count = reply_count = 0

    def protocol_error(exc):
        protocol_errors.append(str(redact(str(exc)))[:1500])

    def keep(chunk, index):
        if len(buffers[index]) + len(chunk) > output_limit:
            exceeded.set()
        else:
            buffers[index].extend(chunk)

    def read(stream, index):
        nonlocal request_count
        pending = b''
        while True:
            chunk = stream.read1(8192)
            if not chunk: break
            if index == 0 and model_request:
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if line.startswith(prefix):
                        try:
                            if len(line) > 1024 * 1024:
                                raise ValueError('Model request exceeds 1 MiB')
                            item = json.loads(line[len(prefix):])
                            if not isinstance(item, dict):
                                raise ValueError('Model request must be an object')
                            requests.put_nowait(item)
                            request_count += 1
                            keep(b'[host model request]\n', index)
                        except (ValueError, RecursionError, queue.Full) as exc:
                            protocol_error(str(exc) or 'Only one model request may be pending')
                    else:
                        keep(line + b'\n', index)
                if len(pending) > 1024 * 1024:
                    protocol_error('Experiment output line exceeds 1 MiB')
                if exceeded.is_set() or protocol_errors:
                    break
                continue
            keep(chunk, index)
            if exceeded.is_set(): break
            if index == 0 and event:
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    try:
                        item = json.loads(line)
                        if item.get('type') == 'item.started' and item.get('item', {}).get('type') in ('command_execution', 'mcp_tool_call', 'file_change'):
                            tools_seen.add(item['item'].get('id'))
                            if len(tools_seen) > tool_limit: exceeded.set()
                        event(redact(item, limit=None))
                    except (ValueError, TypeError): pass
                    except Exception: exceeded.set()
        if index == 0 and model_request and pending:
            if pending.startswith(prefix):
                protocol_error('Model request requires a newline and a waiting process')
            else:
                keep(pending, index)
        stream.close()
    readers = [threading.Thread(target=read, args=(process.stdout, 0), daemon=True),
               threading.Thread(target=read, args=(process.stderr, 1), daemon=True)]
    for thread in readers: thread.start()
    def feed():
        nonlocal reply_count
        try:
            if stdin:
                process.stdin.write(stdin.encode('utf-8')); process.stdin.flush()
            if model_request:
                while not finished.is_set():
                    try: response = replies.get(timeout=.1)
                    except queue.Empty: continue
                    process.stdin.write(response); process.stdin.flush()
                    reply_count += 1
        except (BrokenPipeError, OSError): pass
        finally: process.stdin.close()
    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    reason = ''
    def supervise():
        nonlocal reason
        while not finished.wait(.05):
            try:
                if cancelled(): reason = 'cancelled'
                elif time.monotonic() - started >= timeout: reason = 'timeout'
                elif exceeded.is_set(): reason = 'output_or_tool_budget'
                elif protocol_errors: reason = 'model_protocol_error'
            except Exception as exc:
                protocol_error(exc)
                reason = 'supervision_error'
            if reason or process.poll() is not None:
                close_job()
                return
    supervisor = threading.Thread(target=supervise, daemon=True)
    supervisor.start()
    try:
        while process.poll() is None:
            if reason: break
            if model_request:
                try: request = requests.get_nowait()
                except queue.Empty: request = None
                if request is not None:
                    try:
                        response = model_request(request, started + timeout)
                        if not reason and process.poll() is None:
                            replies.put_nowait((json.dumps(response, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))
                    except Exception as exc:
                        protocol_error(exc)
                        reason = reason or 'model_protocol_error'; break
            time.sleep(0.05)
    finally:
        finished.set()
        supervisor.join(timeout=3)
        close_job()  # Also cleans descendants after a normally exited parent.
        process.wait(timeout=10)
        for thread in readers: thread.join(timeout=3)
        feeder.join(timeout=3)
    if model_request and request_count != reply_count:
        protocol_error('Experiment exited with an unanswered model request')
    if protocol_errors and not reason: reason = 'model_protocol_error'
    if exceeded.is_set() and not reason: reason = 'output_or_tool_budget'
    return {'exit_code': process.returncode, 'termination': reason or ('completed' if process.returncode == 0 else 'nonzero_exit'),
            'seconds': round(time.monotonic() - started, 3), 'tool_calls': len(tools_seen),
            'stdout': buffers[0].decode('utf-8', errors='replace'), 'stderr': buffers[1].decode('utf-8', errors='replace'),
            **({'model_error': protocol_errors[0]} if protocol_errors else {})}
