"""Standalone report pages. A small escaped Markdown subset, with no remote assets."""
from __future__ import annotations

import html
import re
import textwrap
from urllib.parse import urlsplit


TOKEN = re.compile(r'`[^`\n]+`|!?\[[^\]\n]*\]\([^\s)]+\)|https?://[^\s<>"\']+|\[(?:S|E|L)\d+\]')
LIST = re.compile(r'^(\s*)([-+*]|\d+[.)])\s+(.+)')
HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')


def safe_url(value):
    if re.fullmatch(r'/api/spaces/[a-f0-9]{32}/(?:materials/[a-f0-9]{32}/(?:reader|original)(?:#local-L\d+|#page=\d+)?|(?:history|memory)/[a-f0-9]{32}|jobs/[a-f0-9]{32}/report\.(?:html|md))',value):
        return True
    try:
        url = urlsplit(value)
        return url.scheme in {'http', 'https'} and bool(url.hostname) and not url.username and not url.password and not re.search(r'[\s\x00-\x1f\x7f]', value)
    except ValueError:
        return False


def render_markdown(text):
    sources = set(re.findall(r'(?m)^\s*-\s+\[([SL]\d+)\]', text))
    evidence_sources = set(re.findall(r'(?m)^#{1,6}\s+\[(E\d+)\]', text))
    sources.update(evidence_sources)
    source_anchors, headings = set(), []

    def inline(value):
        saved = []
        def token(match):
            raw = match.group()
            markup = html.escape(raw)
            if raw.startswith('`'):
                markup = '<code>' + html.escape(raw[1:-1]) + '</code>'
            elif re.fullmatch(r'\[(?:S|E|L)\d+\]', raw):
                label = raw[1:-1]
                markup = (f'<a class="citation" href="#source-{label}">{raw}</a>' if label in sources
                          else f'<span class="citation">{raw}</span>')
            else:
                link = re.fullmatch(r'!?\[([^\]]*)\]\(([^\s)]+)\)', raw)
                suffix = ''
                if link:
                    label, url = link.groups()
                else:
                    url = raw.rstrip('.,;:!?，。；：！？')
                    while url.endswith(')') and url.count(')') > url.count('('):
                        url = url[:-1]
                    suffix, label = raw[len(url):], url
                if safe_url(url):
                    markup = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>' + html.escape(suffix)
            saved.append(markup)
            return f'\0{len(saved)-1}\0'
        escaped = html.escape(TOKEN.sub(token, str(value).replace('\0', '')))
        escaped = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', escaped)
        escaped = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', escaped)
        return re.sub(r'\0(\d+)\0', lambda m: saved[int(m[1])], escaped)

    def cells(line):
        return re.split(r'(?<!\\)\|', line.strip().strip('|'))

    def table_at(lines, i):
        return i + 1 < len(lines) and '|' in lines[i] and all(re.fullmatch(r'\s*:?-{3,}:?\s*', c) for c in cells(lines[i+1]))

    def block_at(lines, i):
        return bool(HEADING.match(lines[i]) or LIST.match(lines[i]) or lines[i].startswith(('```', '~~~', '>')) or re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*', lines[i]) or table_at(lines, i))

    def blocks(lines, depth=0):
        if depth > 12:
            return '<pre>' + html.escape('\n'.join(lines)) + '</pre>'
        out, i = [], 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1; continue
            if line.startswith(('```', '~~~')):
                fence, code = line[:3], []
                i += 1
                while i < len(lines) and not lines[i].startswith(fence):
                    code.append(lines[i]); i += 1
                out.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
                i += 1; continue
            heading = HEADING.match(line)
            if heading:
                level, label = max(2, len(heading[1])), heading[2]
                anchor = f'section-{len(headings)+1}'
                headings.append((level, anchor, label))
                evidence = re.match(r'^\[(E\d+)\]', label)
                if evidence and evidence[1] not in source_anchors:
                    source_anchors.add(evidence[1]); out.append(f'<span id="source-{evidence[1]}"></span>')
                out.append(f'<h{level} id="{anchor}">{inline(label)}</h{level}>')
                i += 1; continue
            if table_at(lines, i):
                header = ''.join('<th scope="col">' + inline(c.strip().replace(r'\|', '|')) + '</th>' for c in cells(line))
                i += 2; rows = []
                while i < len(lines) and lines[i].strip() and '|' in lines[i]:
                    rows.append('<tr>' + ''.join('<td>' + inline(c.strip().replace(r'\|', '|')) + '</td>' for c in cells(lines[i])) + '</tr>')
                    i += 1
                out.append('<div class="table-wrap" role="region" aria-label="报告表格" tabindex="0"><table><thead><tr>' + header + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
                continue
            if re.fullmatch(r'\s*(?:-{3,}|\*{3,}|_{3,})\s*', line):
                out.append('<hr>'); i += 1; continue
            if line.startswith('>'):
                quote = []
                while i < len(lines) and lines[i].startswith('>'):
                    quote.append(re.sub(r'^>\s?', '', lines[i])); i += 1
                out.append('<blockquote>' + blocks(quote, depth+1) + '</blockquote>'); continue
            item = LIST.match(line)
            if item:
                indent, ordered = len(item[1]), item[2][0].isdigit()
                tag = 'ol' if ordered else 'ul'
                start = f' start="{int(item[2][:-1])}"' if ordered else ''
                entries = []
                while i < len(lines):
                    item = LIST.match(lines[i])
                    if not item or len(item[1]) != indent or item[2][0].isdigit() != ordered:
                        break
                    content, children = item[3], []
                    i += 1
                    while i < len(lines):
                        following = lines[i]
                        if not following.strip():
                            if i+1 < len(lines) and len(lines[i+1])-len(lines[i+1].lstrip()) > indent:
                                children.append(''); i += 1; continue
                            break
                        if len(following)-len(following.lstrip()) <= indent:
                            break
                        children.append(following); i += 1
                    source = re.match(r'^\[([SL]\d+)\]', content)
                    anchor = ''
                    if source and source[1] not in source_anchors:
                        source_anchors.add(source[1]); anchor = f' id="source-{source[1]}"'
                    child = blocks(textwrap.dedent('\n'.join(children)).splitlines(), depth+1) if children else ''
                    entries.append(f'<li{anchor}>' + inline(content) + child + '</li>')
                out.append(f'<{tag}{start}>' + ''.join(entries) + f'</{tag}>'); continue
            paragraph = [line]; i += 1
            while i < len(lines) and lines[i].strip() and not block_at(lines, i):
                paragraph.append(lines[i]); i += 1
            out.append('<p>' + '<br>'.join(inline(row) for row in paragraph) + '</p>')
        return '\n'.join(out)

    markup = blocks(text.replace('\r', '').splitlines())
    return markup, headings


REPORT_STYLE = """
:root{color-scheme:light;font:20px/1.9 "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;color:#382c22;background:#30251e}
*{box-sizing:border-box}body{margin:0}a{color:#835020;text-underline-offset:4px;overflow-wrap:anywhere}button{font:inherit;cursor:pointer}
.report-top{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:20px max(24px,calc((100vw - 1420px)/2));color:#e9d4ae;border-bottom:1px solid #ac85503d;font-size:16px}.report-top strong{font:22px Georgia,serif}.report-top small{margin-left:14px;color:#c9ad82}.print{border:1px solid #b28f59;background:#a7804020;color:#efd6ab;border-radius:5px;padding:7px 17px;font-size:16px}.layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:30px;max-width:1420px;margin:30px auto;padding:0 24px;align-items:start}
.toc{position:sticky;top:24px;max-height:calc(100vh - 48px);overflow:auto;font-size:15px;line-height:1.7;padding:12px 8px;color:#dbc499}.toc h2{font-size:16px;letter-spacing:2px;margin:0 0 18px;color:#dbc499;border:0;padding:0}.toc a{display:block;color:#c9b99e;text-decoration:none;padding:7px 10px;border-left:1px solid #a17e473d}.toc a:hover{color:#ffe8bb;background:#d1ab5b13}.toc .sub{font-size:14px;padding-left:22px}
article{min-width:0;background:#fbf4e5;border:1px solid #ceb990;border-radius:6px;padding:42px 48px 60px;box-shadow:0 10px 40px #110c073d}.cover{border-bottom:1px solid #d8c5a5;padding-bottom:26px;margin-bottom:34px}.eyebrow{font-size:12px;letter-spacing:3px;color:#947243;margin:0 0 15px}h1{font-size:32px;line-height:1.55;margin:0;font-family:Georgia,"Noto Serif SC",SimSun,serif;color:#513921;overflow-wrap:anywhere}.cover-note{font-size:15px;color:#847052;margin:15px 0 0}
.report-body{overflow-wrap:anywhere}h2,h3,h4,h5,h6{font-family:Georgia,"Noto Serif SC",SimSun,serif;line-height:1.55;color:#674623;scroll-margin-top:28px}h2{font-size:27px;border-bottom:1px solid #dbc7a5;padding-bottom:10px;margin:42px 0 22px}h3{font-size:23px;margin:30px 0 16px}h4,h5,h6{font-size:21px}p{margin:0 0 22px}ul,ol{padding-left:1.5em;margin:16px 0 26px}li{margin:9px 0}li:target{background:#eed798;outline:6px solid #eed798;scroll-margin-top:32px}blockquote{margin:24px 0;padding:14px 22px;border-left:4px solid #b48b4c;background:#eee2c960;color:#715c43}blockquote p:last-child{margin-bottom:0}.citation{font-size:.84em;color:#87551f;white-space:nowrap}a.citation{text-decoration:none;background:#ecdfc5;padding:1px 4px;border-radius:3px}
code{font: .85em/1.65 Consolas,monospace;background:#eee1c9;padding:2px 5px;border-radius:3px}pre{font-size:16px;white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;background:#eee1c9;border:1px solid #dcc7a3;padding:20px;border-radius:5px}pre code{padding:0;background:none}.table-wrap{overflow:auto;margin:25px 0;border:1px solid #ccb48c;border-radius:4px}table{border-collapse:collapse;width:100%;min-width:540px;font-size:17px;line-height:1.75}th,td{border:1px solid #d7c5a3;padding:12px 15px;text-align:left;vertical-align:top}th{background:#e7d7b7;font-weight:650}tbody tr:nth-child(even){background:#f4e9d5}hr{border:0;border-top:1px solid #d8c5a5;margin:30px 0}footer{border-top:1px solid #d8c5a5;margin-top:42px;padding-top:20px;font-size:14px;color:#8a775a}::selection{background:#d8ba81;color:#30251e}
@media(max-width:1000px){.layout{grid-template-columns:1fr;gap:20px;margin:20px auto;max-width:900px}.toc{position:static;max-height:160px;overscroll-behavior:contain;border:1px solid #ac85503d;border-radius:5px;padding:12px 16px}.toc h2{margin-bottom:6px}.toc a{display:inline-block;border:0;padding:4px 0;margin-right:18px}.toc .sub{display:none}article{padding:30px}h1{font-size:28px}}
@media(max-width:600px){:root{font-size:18px}.report-top{padding:15px 18px}.report-top small{display:none}.layout{padding:0 12px}article{padding:24px 20px}h1{font-size:25px}h2{font-size:23px}h3{font-size:21px}}
@media print{:root{background:#fff;color:#111;font-size:12pt}.report-top,.toc{display:none}.layout{display:block;margin:0;padding:0;max-width:none}article{box-shadow:none;border:0;padding:0;background:#fff}h1{font-size:23pt}h2{font-size:17pt}h3{font-size:15pt}h2,h3{break-after:avoid}tr,pre,blockquote{break-inside:avoid}.table-wrap{overflow:visible}table{min-width:0;font-size:10pt}a{color:inherit}.citation{background:none}}
"""


def render_report(content, title='研究报告'):
    body = content.removeprefix('# 研究报告\n\n')
    body = body.removeprefix(title + '\n\n')
    markup, headings = render_markdown(body)
    toc = ''.join(f'<a class="{"sub" if level > 2 else "section"}" href="#{anchor}">{html.escape(label)}</a>' for level, anchor, label in headings if level <= 3)
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html.escape(title) + ' · 研究报告</title><style>' + REPORT_STYLE + '</style></head><body>'
            '<header class="report-top"><div><strong>ResearchAgent</strong><small>炉边书房 · 研究手记</small></div>'
            '<button class="print" type="button" onclick="window.print()">打印 / 保存 PDF</button></header>'
            '<div class="layout"><nav class="toc" aria-label="报告目录"><h2>本篇目录</h2>' + toc + '</nav>'
            '<article><header class="cover"><p class="eyebrow">RESEARCH NOTE</p><h1>' + html.escape(title) + '</h1>'
            '<p class="cover-note">完整研究报告 · 点击目录跳转章节，点击来源编号回看出处</p></header>'
            '<div class="report-body">' + markup + '</div><footer>ResearchAgent · 研究过程与原始记录保存在工作台中。</footer>'
            '</article></div></body></html>')
