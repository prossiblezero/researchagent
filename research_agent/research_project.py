"""Prepare public project inputs and wheel-only dependencies for a research job."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import sys
import venv
import zipfile
from contextlib import closing
from pathlib import Path

from .coding_tool import regular_file, relative_path
from .experiment_process import run_process, sandbox_command
from .materials import fetch_public, github_repo, read_github
from .trace import redact
from .workbench_store import Conflict, text_field


DOWNLOAD_ROOT = Path('D:/paper/researchagent-auto')


def directory(root, relative=''):
    root.mkdir(parents=True, exist_ok=True)
    paths = [root]
    if relative:
        for component in relative_path(relative).split('/'):
            paths.append(paths[-1] / component)
    for path in paths:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not path.is_dir() or path.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('项目目录不能使用链接或重解析点')
        else:
            path.mkdir()
    return paths[-1]


def write_input(workspace, name, data):
    path = workspace / relative_path(name)
    parent = path.parent.relative_to(workspace).as_posix()
    directory(workspace, '' if parent == '.' else parent)
    if path.exists() or path.is_symlink():
        if regular_file(workspace, name, limit=300 * 1024 * 1024) != data:
            raise Conflict('目标已有不同内容，不覆盖已有代码或实验数据：' + name)
    else:
        with path.open('xb') as stream:
            stream.write(data)


def unpack(data, workspace, destination):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = archive.infolist()
        if len(items) > 10000 or sum(i.file_size for i in items) > 300 * 1024 * 1024:
            raise ValueError('源码包超过10000文件或300MB展开上限')
        top = {i.filename.split('/')[0] for i in items if i.filename}
        strip = next(iter(top)) + '/' if len(top) == 1 and all('/' in i.filename for i in items) else ''
        validated = []
        for item in items:
            if item.is_dir():
                continue
            if item.filename.startswith(('/', '\\')) or re.search(r'[:\\\x00-\x1f]', item.filename) or '..' in item.filename.split('/'):
                raise ValueError('源码包包含越界路径')
            name = item.filename[len(strip):] if strip else item.filename
            if any(part.lower() == '.git' or part.lower() == '.env' or part.lower().startswith('.env.') for part in name.split('/')):
                continue
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('源码包包含链接，未展开')
            relative_path(name)
            target = relative_path(destination + '/' + name)
            validated.append((item, target))
        if len({name.casefold() for _, name in validated}) != len(validated):
            raise ValueError('源码包包含重复或Windows大小写冲突路径')
        for item, target in validated:
            write_input(workspace, target, archive.read(item))
        return [name for _, name in validated]


def python_for(app, job_id):
    record = app.store.path.resolve().parent / 'auto-research' / job_id / 'environment.json'
    if not record.exists():
        return sys.executable
    info = json.loads(record.read_text(encoding='utf-8'))
    workspace = app.coding.work_root / job_id
    executable = workspace / '.venv/Scripts/python.exe'
    if hashlib.sha256(regular_file(workspace, '.venv/Scripts/python.exe')).hexdigest() != info['python_sha256']:
        raise Conflict('项目Python解释器已改变，保留现场并拒绝执行')
    return str(executable)


def prepare(app, job, request, call_id):
    if not isinstance(request, dict) or set(request) != {'sources', 'packages'}:
        raise ValueError('项目准备需要sources和packages')
    sources, packages = request['sources'], request['packages']
    if not isinstance(sources, list) or len(sources) > 6 or not isinstance(packages, list) or len(packages) > 20:
        raise ValueError('每次最多6个公开来源、20项Python依赖')
    if any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}==[A-Za-z0-9][A-Za-z0-9_.+!-]{0,60}', p) for p in packages):
        raise ValueError('依赖须为name==version；不接受脚本、URL、可编辑安装或未固定版本')
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'url', 'kind', 'destination'} or source['kind'] not in {'file', 'zip', 'github'}:
            raise ValueError('来源需要url、kind(file/zip/github)和项目内destination')
        text_field(source['url'], '公开来源URL', 2000)
        relative_path(source['destination'])
        if source['destination'].split('/')[0].lower() == '.venv':
            raise ValueError('公开输入不能覆盖宿主管理的Python环境')
    app.auto_research.current(job)
    authorization = json.loads(job['payload'])['auto_research']
    if (authorization.get('metric_contract') or {}).get('name') == 'locomo_qa_v1' and sources:
        raise Conflict('固定LoCoMo评测只使用宿主预先冻结的来源，不能追加下载可能包含测试标签的数据或仓库')
    with closing(app.store._connect()) as db:
        if db.execute("SELECT 1 FROM coding_tasks WHERE job_id=? AND status IN ('queued','running')", (job['id'],)).fetchone():
            raise Conflict('项目仍在编码或实验中，等待完成后再准备输入')
    config = json.loads(job['payload'])['auto_research']['budget']
    receipts = app.store.path.resolve().parent / 'auto-research' / job['id']
    receipts.mkdir(parents=True, exist_ok=True)
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    key = hashlib.sha256(call_id.encode()).hexdigest()[:24]
    receipt = receipts / ('prepare-' + key + '.json')
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding='utf-8'))
        if previous['request_sha256'] != request_hash:
            raise Conflict('同一准备调用不能更换输入')
        return previous
    result = {'request_sha256': request_hash, 'sources': [], 'packages': packages, 'ok': False}
    if len(list(receipts.glob('prepare-*.json'))) >= config['preparations']:
        raise Conflict('项目准备次数预算已用尽')
    workspace = directory(app.coding.work_root / job['id'])
    cache = directory(DOWNLOAD_ROOT / job['id'])
    max_bytes = config['download_mb'] * 1024 * 1024
    private = app.coding.private_paths(job['id'])

    def checkpoint(message):
        app.auto_research.current(job)
        app.store.progress(job['id'], 'preparing_project', message, str(app.trace_dir / ('auto-' + job['id'] + '.jsonl')))
        app.sessions.event(job, 'project_preparation', {'message': message})

    def size():
        return sum(path.stat().st_size for path in cache.rglob('*') if path.is_file())

    def cancelled():
        try:
            app.auto_research.current(job)
            return size() > max_bytes
        except (Conflict, LookupError):
            return True

    try:
        for source in sources:
            checkpoint('准备公开项目输入：' + source['url'])
            url = source['url']
            metadata = {}
            if source['kind'] == 'github':
                if not github_repo(url):
                    raise ValueError('github来源须为公开仓库主页URL')
                info = read_github(url, checkpoint=lambda _, message: checkpoint(message))
                url, metadata = info['archive_url'], info['metadata']
            name = hashlib.sha256(url.encode()).hexdigest() + '.download'
            cached = cache / name
            if cached.exists():
                data = regular_file(cache, name, limit=max_bytes)
            else:
                remaining = max_bytes - size()
                if remaining <= 0:
                    raise Conflict('下载容量预算已用尽')
                data, media, final_url, _ = fetch_public(url, max_bytes=min(100 * 1024 * 1024, remaining))
                checkpoint('保存公开输入快照')
                write_input(cache, name, data)
                metadata.update(media_type=media, final_url=final_url)
            if source['kind'] in {'zip', 'github'}:
                files = unpack(data, workspace, source['destination'])
            else:
                write_input(workspace, source['destination'], data)
                files = [source['destination']]
            result['sources'].append({**source, 'sha256': hashlib.sha256(data).hexdigest(), 'metadata': metadata,
                                      'download_path': str(cached), 'files': files[:200], 'file_count': len(files)})
        if packages:
            checkpoint('准备独立Python环境与固定版本依赖')
            environment = receipts / 'environment.json'
            envroot = workspace / '.venv'
            if not environment.exists():
                if envroot.exists() or envroot.is_symlink():
                    raise Conflict('已有未登记的.venv，保留现有文件，不覆盖或在宿主运行它')
                # No environment interpreter is ever executed on the host: ensurepip/install run in the native sandbox.
                build = receipts / 'runtime-build'
                venv.EnvBuilder(with_pip=False, symlinks=False).create(build)
                shutil.copytree(build, envroot)
                sha = hashlib.sha256(regular_file(workspace, '.venv/Scripts/python.exe')).hexdigest()
                environment.write_text(json.dumps({'python_sha256': sha, 'packages': []}), encoding='utf-8')
            python = python_for(app, job['id'])
            wheels = directory(cache, 'wheels')
            steps = [
                (True, [python, '-I', '-m', 'ensurepip', '--upgrade']),
                (False, [sys.executable, '-I', '-m', 'pip', '--isolated', 'download', '--disable-pip-version-check', '--no-input',
                         '--only-binary=:all:', '--index-url', 'https://pypi.org/simple', '--dest', str(wheels), *packages]),
                (True, [python, '-I', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--no-input',
                        '--no-index', '--only-binary=:all:', '--find-links', str(wheels), *packages]),
            ]
            result['environment_steps'] = []
            for isolated, command in steps:
                checkpoint('安装固定版本wheel依赖（不执行源码安装脚本）')
                actual = sandbox_command(workspace, command, private) if isolated else command
                response = run_process(actual, workspace if isolated else cache, timeout=120, cancelled=cancelled)
                result['environment_steps'].append(response)
                if response['exit_code'] != 0 or response['termination'] != 'completed':
                    raise RuntimeError('依赖准备失败：' + response['termination'])
            info = json.loads(environment.read_text(encoding='utf-8'))
            info['packages'] = list(dict.fromkeys(info['packages'] + packages))
            environment.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
        checkpoint('项目输入已准备，等待研究模型选择下一步')
        result['ok'] = True
    except Exception as exc:
        result['error'] = str(redact(str(exc)))[:1800]
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(receipt)
    return result
