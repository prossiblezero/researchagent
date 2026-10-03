"""Bounded public downloads and page-aware document extraction. Never execute source code."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from .search import canonical_url, resolve_public_addresses, _request_once, _media_type, extract_html_text

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT = 500_000
PUBLIC_TYPES = {'application/pdf', 'application/octet-stream', 'text/html', 'text/plain', 'text/markdown', 'application/json', 'application/zip', 'application/x-zip-compressed'}
SECTIONS = {
    'abstract': r'abstract|摘要', 'introduction': r'introduction|引言|绪论',
    'method': r'(?:our |proposed )?(?:method\w*|approach|framework|model|architecture)|方法|模型|设计',
    'experiment': r'experiment\w*|experimental (?:setup|design|evaluation)|evaluation|实验(?:设计|设置)?|评测',
    'result': r'results?(?: and discussion)?|discussion|结果|讨论',
    'limitation': r'limitations?(?: and future work)?|局限\w*|不足',
    'conclusion': r'conclusions?|结论', 'references': r'references|bibliography|参考文献',
}


def fetch_public(url, max_bytes=MAX_FILE_BYTES, timeout=25):
    """Pin every hop to checked public IPs; reject partial/non-document responses."""
    current = canonical_url(url)
    if not current:
        raise ValueError('只允许无凭据的公网 HTTP(S) 地址')
    for hop in range(4):
        parsed = urlsplit(current)
        addresses = resolve_public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        response = _request_once(current, addresses, timeout, max_bytes, PUBLIC_TYPES)
        if response.status in {301, 302, 303, 307, 308}:
            target = canonical_url(urljoin(current, response.headers.get('location', '')))
            if not response.headers.get('location') or not target:
                raise ValueError('下载重定向地址不安全')
            current = target
            continue
        if response.status != 200:
            raise ValueError(f'来源不可访问（HTTP {response.status}）；未下载或声称已通读')
        media, charset = _media_type(response.headers.get('content-type', ''))
        if media not in PUBLIC_TYPES:
            raise ValueError('来源不是支持的 PDF、文本或仓库文件：' + media)
        if response.truncated:
            raise ValueError(f'文件超过本次下载限额（{max_bytes // 1024 // 1024} MB），未保存半文件')
        data = response.body
        encoding = response.headers.get('content-encoding', '').lower().strip()
        if encoding in {'gzip', 'x-gzip'} or data.startswith(b'\x1f\x8b'):
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = decoder.decompress(data, max_bytes + 1)
            if len(data) > max_bytes or decoder.unused_data or not decoder.eof:
                raise ValueError('压缩文件不完整或解压后超过限额')
        elif encoding not in {'', 'identity'}:
            raise ValueError('来源使用了不支持的内容压缩方式')
        if not data:
            raise ValueError('来源返回空文件')
        return data, media, current, charset
    raise ValueError('来源重定向过多')


class PaperHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.meta = {}; self.links = []; self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            name = (attrs.get('name') or attrs.get('property') or '').lower()
            if name and attrs.get('content'):
                self.meta.setdefault(name, []).append(attrs['content'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])


def identifier(url, metadata=None):
    parsed = urlsplit(url)
    if (parsed.hostname or '').lower() in {'arxiv.org', 'export.arxiv.org', 'www.arxiv.org'}:
        match = re.match(r'^/(?:abs|pdf|html)/(.+?)(?:\.pdf)?$', parsed.path)
        if match:
            return 'arxiv:' + re.sub(r'v\d+$', '', match[1]).lower()
    doi = (metadata or {}).get('doi', '')
    if not doi and (parsed.hostname or '').lower() in {'doi.org', 'dx.doi.org'}:
        doi = unquote(parsed.path.lstrip('/'))
    doi = doi.removeprefix('https://doi.org/').strip()
    if re.fullmatch(r'10\.\d{4,9}/\S+',doi,re.I):
        return 'doi:' + doi.lower().removeprefix('https://doi.org/').strip()
    return canonical_url(url) or ''


def section_name(line):
    clean = re.sub(r'^\s*(?:#{1,6}\s*|(?:\d+(?:\.\d+)*|[IVX]+)[.)]?\s+)', '', line).strip().strip(':：')
    if len(clean) > 90:
        return None
    for name, pattern in SECTIONS.items():
        if re.fullmatch(pattern, clean, re.I):
            return name
    return None


def split_pages(pages):
    chunks, section = [], '正文'
    for page_number, text in pages:
        buffer, start = [], 1
        def flush(end):
            if buffer and ''.join(buffer).strip():
                chunks.append({'page': page_number, 'section': section, 'line_start': start,
                               'line_end': end, 'text': '\n'.join(buffer).strip()})
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            heading = section_name(line)
            if heading or sum(map(len, buffer)) + len(line) > 1800:
                flush(i - 1); buffer = []; start = i
            if heading:
                section = heading
            # Bound a single long line as well (minified pages/logs).
            while len(line) > 1800:
                flush(i); buffer = []; start = i
                chunks.append({'page':page_number,'section':section,'line_start':i,'line_end':i,'text':line[:1800]})
                line = line[1800:]
            buffer.append(line)
        flush(len(lines))
    return chunks


def parse_pdf(data):
    if not data.startswith(b'%PDF-'):
        raise ValueError('链接返回的不是 PDF（可能是付费、登录或错误页面），未保存为论文')
    # ponytail: one parser subprocess with a 60s deadline; a sandboxed OCR service only if scanned papers become necessary.
    with tempfile.TemporaryDirectory(prefix='research-pdf-') as folder:
        source = Path(folder) / 'input.pdf'; source.write_bytes(data)
        result = subprocess.run([sys.executable, '-B', '-m', 'research_agent.materials', str(source)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('PDF 无法解析（损坏、加密或超出解析限制），未声称已通读')
        return json.loads(result.stdout.decode('utf-8'))


def extract_pdf_in_process(path):
    import logging
    from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    reader = PdfReader(path, strict=False)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('Encrypted PDF')
    pages, warnings, total = [], [], 0
    count = len(reader.pages)
    for index, page in enumerate(reader.pages[:200], 1):
        try:
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > 8_000_000:
                raise ValueError('page content too large')
            text = page.extract_text() or ''
            if len(text.strip()) < 35:
                warnings.append(f'第 {index} 页：文字不足，可能是扫描页/图表页；未做 OCR')
            if '\ufffd' in text:
                warnings.append(f'第 {index} 页：部分字符无法解码')
            text = text[:MAX_TEXT-total]
            pages.append((index, text)); total += len(text)
            if total >= MAX_TEXT:
                warnings.append(f'从第 {index} 页起达到文本限额，后续未解析'); break
        except Exception:
            warnings.append(f'第 {index} 页：提取失败，未解析此页'); pages.append((index, ''))
    if count > 200:
        warnings.append('超过 200 页，仅解析前 200 页')
    metadata = reader.metadata or {}
    return {'pages':pages,'page_count':count,'title':str(metadata.get('/Title') or '')[:400],
            'authors':[str(metadata.get('/Author'))[:500]] if metadata.get('/Author') else [],
            'warnings':warnings, 'boundary':'仅提取文字层；未验证任何页中的公式、表格结构和图片。引用页码可回到原 PDF 核对。'}


def github_repo(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname != 'github.com' or len(parts) != 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix('.git')
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in {'.','..'} for p in (owner,repo)):
        return None
    return owner, repo


def read_github(url, fetch=fetch_public, checkpoint=lambda *args: None):
    owner, repo = github_repo(url)
    base = f'https://api.github.com/repos/{owner}/{repo}'
    def get(suffix):
        checkpoint('reading', '读取 GitHub ' + suffix)
        return json.loads(fetch(base + suffix, max_bytes=2_000_000)[0])
    info = get('')
    commit = get('/commits/' + quote(info['default_branch'], safe=''))['sha']
    tree = get('/git/trees/' + commit + '?recursive=1')
    files = [x for x in tree.get('tree', []) if x.get('type') == 'blob']
    warnings = ['只读分析仓库文件，未安装依赖、执行代码或验证测试通过。']
    if tree.get('truncated'):
        warnings.append('GitHub 返回的目录树被截断')
    texts = [('仓库概况', f"{owner}/{repo}\nCommit: {commit}\n{info.get('description') or ''}"),
             ('目录树', '\n'.join(x['path'] for x in files[:1200]))]
    priority = []
    for item in files:
        path = item['path']; name = path.rsplit('/',1)[-1].lower()
        rank = 0 if name.startswith('readme') and '/' not in path else 1 if name in {'pyproject.toml','package.json','requirements.txt','cargo.toml','go.mod'} else 2 if name in {'main.py','app.py','server.py','__main__.py','index.ts','index.js','cli.py'} else 3 if re.search(r'(^|/)(tests?)/',path) else 4 if re.search(r'\.(py|ts|js|rs|go)$',path) and name not in {'__init__.py','conftest.py'} else 9
        if rank < 9 and item.get('size',0) <= 120_000 and re.search(r'\.(md|txt|py|toml|json|js|ts|rs|go)$',path,re.I):
            priority.append((rank, len(path), path))
    selected=[]
    for rank,limit in ((0,1),(1,2),(2,2),(3,2),(4,3)):
        selected.extend([x for x in sorted(priority) if x[0]==rank][:limit])
    for _, _, path in selected:
        checkpoint('reading', '读取仓库文件：' + path)
        try:
            record = get('/contents/' + quote(path, safe='/') + '?ref=' + commit)
            if record.get('encoding') != 'base64' or record.get('type') != 'file':
                raise ValueError('GitHub 未返回可读取的文件正文')
            data = base64.b64decode(''.join(record.get('content','').split()), validate=True)
            if len(data) > 150_000:
                raise ValueError('仓库文件超过单文件阅读限额')
            texts.append((path, data.decode('utf-8','replace')))
        except (OSError, ValueError, binascii.Error) as exc:
            warnings.append(path + '：读取失败 ' + str(exc)[:120])
    chunks = []
    for path, text in texts:
        for chunk in split_pages([(None,text)]):
            chunk['section'] = path + ' · ' + chunk['section']; chunks.append(chunk)
    return {'kind':'github','title':f'{owner}/{repo}','url':url,'canonical_id':f'github:{owner.lower()}/{repo.lower()}@{commit}',
            'metadata':{'commit':commit,'default_branch':info['default_branch'],'file_count':len(files),'files_read':[x[0] for x in texts[2:]],'publication_status':'不适用'},
            'chunks':chunks,'warnings':warnings,'boundary':'目录和最多 10 个关键文本文件的只读分析，未通读整个仓库。',
            'data':None, 'filename':'source.zip','archive_url':f'https://codeload.github.com/{owner}/{repo}/zip/{commit}'}


def read_material(url, fetch=fetch_public, checkpoint=lambda *args: None, max_bytes=MAX_FILE_BYTES):
    if not canonical_url(url):
        raise ValueError('资料地址必须是公网 HTTP(S) URL')
    if github_repo(url):
        return read_github(url,fetch,checkpoint)
    checkpoint('downloading','获取来源：' + url)
    data, media, final_url, charset = fetch(url,max_bytes=max_bytes)
    meta, title, pdf_url = {}, '', ''
    if media == 'text/html':
        text = data.decode(charset,'replace'); parser = PaperHTML(text)
        def first(*keys):
            return next((parser.meta[k][0] for k in keys if parser.meta.get(k)), '')
        title = first('citation_title','dc.title','og:title') or extract_html_text(text)[0]
        meta = {'title':title,'authors':parser.meta.get('citation_author',[]),
                'year':first('citation_publication_date','citation_date','dc.date')[:4],
                'venue':first('citation_conference_title','citation_journal_title'),
                'doi':first('citation_doi','dc.identifier'),'metadata_source':final_url}
        meta['publication_status'] = '来源声明发表，待交叉核验' if meta['venue'] else '发表状态未核验'
        if identifier(url).startswith('arxiv:'):
            meta['arxiv_id'] = identifier(url)[6:]; meta['publication_status'] = 'arXiv 预印本（不代表正式发表）'
        observed = first('citation_pdf_url','wkhealth_pdf_url')
        if not observed and (first('citation_title') or identifier(url).startswith('arxiv:')):
            observed = next((x for x in parser.links if urlsplit(x).path.lower().endswith('.pdf') or
                            (identifier(url).startswith('arxiv:') and urlsplit(urljoin(final_url,x)).path.startswith('/pdf/') and identifier(urljoin(final_url,x))==identifier(url))), '')
        if observed:
            pdf_url = urljoin(final_url,observed)
        if pdf_url:
            checkpoint('downloading','下载原文 PDF：' + pdf_url)
            data, media, final_url, charset = fetch(pdf_url,max_bytes=max_bytes)
            if not data.startswith(b'%PDF-'):
                raise ValueError('原文链接返回非 PDF 内容；可能需要付费或登录')
    if media == 'application/pdf' or data.startswith(b'%PDF-'):
        checkpoint('extracting','按页提取 PDF 文字和章节')
        parsed = parse_pdf(data)
        meta['authors'] = meta.get('authors') or parsed['authors']
        meta['page_count'] = parsed['page_count']; meta['pdf_url'] = final_url
        if identifier(url).startswith('arxiv:'):
            meta['arxiv_id'] = identifier(url)[6:]; meta['publication_status'] = 'arXiv 预印本（不代表正式发表）'
        return {'kind':'paper','title':title or parsed['title'] or unquote(urlsplit(url).path.rsplit('/',1)[-1]),
                'url':url,'canonical_id':identifier(url,meta),'metadata':meta,'chunks':split_pages(parsed['pages']),
                'warnings':parsed['warnings'],'boundary':parsed['boundary'],'data':data,'filename':'原文.pdf'}
    if media not in {'text/html','text/plain','text/markdown'}:
        raise ValueError('此地址没有可阅读的论文或文本；未将二进制内容当正文')
    text = data.decode(charset,'replace')
    web_title, text = extract_html_text(text) if media == 'text/html' else ('',text)
    return {'kind':'document','title':title or web_title or final_url,'url':url,'canonical_id':identifier(url,meta),
            'metadata':meta,'chunks':split_pages([(None,text[:MAX_TEXT])]),
            'warnings':['文本超过限额，尾部未索引'] if len(text)>MAX_TEXT else [],'boundary':'网页正文；不包括链接到的其他页面和文件。',
            'data':text.encode('utf-8'),'filename':'原文.txt'}


def safe_path(root, path):
    root = Path(root).resolve()
    candidate = Path(path)
    if any(part in {'..'} for part in candidate.parts):
        raise ValueError('路径不能包含 ..')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise PermissionError('资料路径不在当前研究区授权的目录内')
    return resolved


def slug(text, limit=55):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(text)).strip(' .')[:limit].rstrip(' .') or '资料'
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        value = '_' + value
    return value


def atomic_write(root, path, data, *, replace=False):
    target = safe_path(root,path)
    target.parent.mkdir(parents=True,exist_ok=True)
    safe_path(root,target)
    if target.exists() and not replace:
        raise FileExistsError('目标文件已存在，未覆盖：' + str(target))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,suffix='.part',delete=False) as stream:
            temporary = Path(stream.name); stream.write(data)
        if replace:
            temporary.replace(target)
        else:
            # Atomically publish a complete file without clobbering a racing writer, on NTFS and POSIX.
            os.link(temporary,target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    print(json.dumps(extract_pdf_in_process(sys.argv[1]),ensure_ascii=True))
