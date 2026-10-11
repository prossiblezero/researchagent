"""Derived, source-bound author and citation lookup for the current paper library."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from contextlib import closing
from urllib.parse import quote, urlencode

from .trace import redact
from .search import canonical_url


TOOL = {'type': 'function', 'function': {
    'name': 'lookup_papers',
    'description': 'Look up explicit author/citation relationships in the current saved paper library. Modes: authors lists paper bylines; author_papers finds exact full-name candidates, optionally compared with one seed paper; references/cited_by follow directed citations; shared_references finds targets cited by every selected paper. artifact_ids are owner_id values from retrieve. Returns original ref_ids to read_evidence, not final factual evidence. Same names or initials do not establish identity; inspect identity_match, shared context and original bylines. Coverage is limited to saved, readable papers and recognizable bylines/references; a missing edge does not prove absence. No network access.',
    'parameters': {'type': 'object', 'properties': {
        'mode': {'type': 'string', 'enum': ['authors', 'author_papers', 'references', 'cited_by', 'shared_references']},
        'artifact_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20},
        'author': {'type': 'string', 'description': 'Full name as written in a byline; never expand initials by guessing.'},
        'query': {'type': 'string', 'description': 'Optional paper-title or author-name filter.'},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12}
    }, 'required': ['mode'], 'additionalProperties': False}
}}

DISCOVERY_TOOL = {'type': 'function', 'function': {
    'name': 'discover_author_papers',
    'description': 'Online author discovery anchored to one saved seed paper DOI and a full name verified in its byline. Resolves OpenAlex author candidates on that exact DOI, then lists works for the selected author ID. When the user specifies a field, pass a short topic query and request enough candidates (per_page up to 10). Read relevant candidates before more broad searches. Pass an author_id only from this seed response; multiple candidates require selecting one. Provider author IDs can be incomplete or incorrectly merged: verify each candidate against original authors, affiliations and coauthors using the returned source URLs. Does not import papers or prove a complete bibliography.',
    'parameters': {'type': 'object', 'properties': {
        'seed_artifact_id': {'type': 'string'}, 'author': {'type': 'string'},
        'author_id': {'type': 'string', 'description': 'OpenAlex author ID from this seed; omit to select a unique match.'},
        'query': {'type': 'string', 'description': 'Optional topic filter, for example multi-view clustering.'},
        'page': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'per_page': {'type': 'integer', 'minimum': 1, 'maximum': 10}
    }, 'required': ['seed_artifact_id', 'author'], 'additionalProperties': False}
}}


def discovery_progress(cache):
    """Rebuild bounded acquisition state from durable tool receipts, including on resume."""
    candidates = {}
    reads = {p.get('url'): p for (name, _), p in cache.items() if name == 'read'}
    searched = [s for (name, _), p in cache.items() if name == 'search' for s in p.get('results', [])]
    for (name, _), payload in cache.items():
        if name != 'discover_author_papers':
            continue
        for item in payload.get('results', []):
            key = item.get('doi') or item.get('work_id') or item.get('title')
            candidates[key] = item
    progress = []
    for item in candidates.values():
        title = str(item.get('title') or '')
        urls = list(item.get('urls', []))
        for source in searched:
            if normalized(title) and normalized(title) == normalized(str(source.get('title') or '')):
                urls.append(source['url'])
        urls = list(dict.fromkeys(urls))
        originals = [reads[u]['evidence_id'] for u in urls if reads.get(u, {}).get('ok') and reads[u].get('evidence_id')]
        progress.append({'title': title, 'doi': item.get('doi'), 'year': item.get('publication_year'),
            'venue': item.get('venue'), 'identity_check': item.get('identity_check'),
            'shared_coauthors': item.get('shared_coauthors', []),
            'original_evidence_ids': originals,
            'reading_status': 'read_requires_verification' if originals else 'original_missing',
            'failed_urls': [{'url': u, 'code': (reads[u].get('error') or {}).get('code')}
                            for u in urls if u in reads and not reads[u].get('ok')],
            'next_urls': [u for u in urls if u not in reads][:2]})
    return progress[:10]


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS paper_relation_state (
      space_id TEXT PRIMARY KEY REFERENCES research_spaces(id) ON DELETE CASCADE,
      fingerprint TEXT NOT NULL, coverage TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS paper_authorships (
      artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
      name_key TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(artifact_id,name_key));
    CREATE INDEX IF NOT EXISTS idx_paper_author_name ON paper_authorships(name_key);
    CREATE TABLE IF NOT EXISTS paper_citation_edges (
      source_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
      target_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
      detail TEXT NOT NULL, PRIMARY KEY(source_id,target_id));
    CREATE INDEX IF NOT EXISTS idx_paper_cited_by ON paper_citation_edges(target_id);
    ''')


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def name_key(value):
    parts = value.split(',')
    if len(parts) == 2:
        value = parts[1] + ' ' + parts[0]
    return normalized(value)


def name_words(value):
    text = ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if not unicodedata.combining(c))
    return ' '.join(''.join(c if c.isalpha() else ' ' for c in text).split())


def byline_names(metadata):
    values = metadata.get('authors', [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, dict):
            name = value.get('name') or ' '.join(str(value.get(k) or '') for k in ('given', 'family')).strip()
            orcid = str(value.get('orcid') or value.get('ORCID') or '').removeprefix('https://orcid.org/')
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                continue
            if re.fullmatch(r'\d{4}-\d{4}-\d{4}-\d{3}[\dX]', orcid):
                result.append((name, orcid))
            else:
                result.append((name, ''))
        elif isinstance(value, str):
            parts = re.split(r';|\s+and\s+', value)
            for part in parts:
                comma = part.split(',')
                # A PDF /Author field often holds the entire byline. Preserve "Family, Given".
                names = comma if len(comma) > 1 and all(len(p.split()) >= 2 for p in comma) else [part]
                result.extend((n.strip(), '') for n in names if n.strip() and len(n) <= 200)
    return list(dict.fromkeys(result))


def anchor(chunk):
    return {'ref_id': 'D' + str(chunk['id']) + '-0', 'page': chunk['page'],
            'section': chunk['section'], 'chunk_id': chunk['id']}


def front_matter_names(text, title):
    """Recover clear Latin-script bylines; ambiguous layouts stay unresolved."""
    text = re.split(r'\bAbstract\b|A\s+B\s+S\s+T\s+R\s+A\s+C\s+T|\bIntroduction\b', text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r'\n\s*,', ',', text)
    text = re.sub(r',\s*\n', ', ', text)
    names = []
    for line in text.splitlines():
        if '@' in line or normalized(line) in normalized(title):
            continue
        # IEEE membership labels and printed affiliation marks are not names.
        line = re.sub(r'\b(?:(?:Senior|Life|Student)\s+)?(?:Member|Fellow)\s*,?\s*IEEE\b', '', line)
        parts = [p.strip() for p in re.split(r'[,;]|\band\b|&', line) if p.strip()]
        candidates = []
        for part in parts:
            part = re.sub(r'[\d*∗†‡¹²³⁴⁵]+', '', part).strip()
            part = re.sub(r'\s+[a-z]$', '', part).strip()
            if not part:
                continue
            words = part.split()
            if (not 2 <= len(words) <= 5 or any(
                    not re.fullmatch(r"[^\W\d_][^\W\d_.'’\-]*\.?", w) or
                    not (w[0].isupper() or w in {'de', 'del', 'van', 'von', 'da', 'der'}) for w in words)
                    or re.search(r'\b(?:University|Institute|College|School|Academy|Laboratory|Hospital|Department|Journal|Proceedings)\b', part, re.I)):
                candidates = []
                break
            candidates.append(' '.join(words))
        # Requiring a multi-person line avoids guessing that a title is an author.
        if len(candidates) >= 2:
            names.extend(candidates)
    return list(dict.fromkeys(names))


def authors(paper, chunks):
    header = []
    for chunk in chunks:
        if (chunk['page'] or 1) > 1:
            break
        if sum(len(c['content']) for c in header) + len(chunk['content']) > 14000:
            break
        header.append(chunk)
    text = '\n'.join(c['content'] for c in header)
    # Exclude the title and institution/contact lines before confirming a name in the byline.
    front = re.split(r'\bAbstract\b|A\s+B\s+S\s+T\s+R\s+A\s+C\s+T|\bIntroduction\b', text, maxsplit=1, flags=re.I)[0]
    byline = '\n'.join(line for line in front.splitlines()
                       if '@' not in line and not re.search(r'\b(?:university|institute|college|school|academy|laboratory)\b|大学|学院|研究所', line, re.I))
    clean = name_words(byline).replace(name_words(paper['title']), '', 1)
    expanded = re.sub(r'\{([^{}]+)\}@([\w.-]+)',
                      lambda m: ' '.join(n.strip() + '@' + m[2] for n in m[1].split(',')), text)
    emails = re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', expanded)
    institutions = list(dict.fromkeys(line.strip() for line in text.splitlines()
                        if re.search(r'\b(?:university|institute|college|school|academy|laboratory)\b|大学|学院|研究所', line, re.I)))
    found = []
    metadata_names = byline_names(paper['metadata'])
    extracted = front_matter_names(front, paper['title'])
    candidates = [(name, next((o for n, o in metadata_names if name_key(n) == name_key(name)), '')) for name in extracted]
    candidates += [(n, o) for n, o in metadata_names if name_key(n) not in {name_key(x) for x in extracted}]
    for name, orcid in candidates:
        key = name_key(name)
        parts = name.split(',')
        words = name_words(parts[1] + ' ' + parts[0] if len(parts) == 2 else name)
        # Metadata alone, bibliography occurrences, and initial expansion do not prove authorship.
        if len(key) < 3 or ' ' + words + ' ' not in ' ' + clean + ' ':
            continue
        found.append({'name': name, 'name_key': key, 'position': clean.find(words), 'orcid': orcid,
                      'emails': sorted({e.casefold() for e in emails if normalized(e.split('@')[0]) == key}),
                      'paper_affiliations': institutions[:12], 'evidence': [anchor(c) for c in header],
                      'metadata_source': paper['metadata'].get('metadata_source', ''),
                      'name_source': 'original_byline' if name in extracted else 'metadata_confirmed_in_original'})
    found.sort(key=lambda a: a['position'])
    for position, person in enumerate(found, 1):
        person['position'] = position
    return found


def reference_matches(chunks, targets, *, min_title_length=24):
    groups = []
    for chunk in chunks:
        if chunk['section'] == 'references':
            if not groups or groups[-1][-1]['ordinal'] + 1 != chunk['ordinal']:
                groups.append([])
            groups[-1].append(chunk)
    matches = {}
    # ponytail: exact title scans suit small paper libraries; use an inverted title index if scale requires it.
    for group in groups:
        text = '\n'.join(c['content'] for c in group)
        clean, positions = [], []
        for pos, char in enumerate(text):
            for c in unicodedata.normalize('NFKD', char).casefold():
                if c.isalnum():
                    clean.append(c); positions.append(pos)
        clean = ''.join(clean)
        spans, offset = [], 0
        for chunk in group:
            spans.append((offset, offset + len(chunk['content']), chunk))
            offset += len(chunk['content']) + 1
        for target in targets:
            title = normalized(target['title'])
            doi = str(target['metadata'].get('doi') or '')
            candidates = [('doi', normalized(doi))] if re.fullmatch(r'10\.\d{4,9}/\S+', doi) else []
            if len(title) >= min_title_length:
                candidates.append(('title', title))
            for kind, needle in candidates:
                start = clean.find(needle)
                while start >= 0:
                    left, right = positions[start], positions[start + len(needle) - 1] + 1
                    tail = text[right:]
                    # A title cannot be the trailing words of a longer title, even
                    # when that title wraps across lines. Ambiguous layouts stay unindexed.
                    head = text[:left].rstrip()
                    title_start = (not head or head[-1] in '.!?;)]“\"‘'
                        or re.search(r'(?:^|\n)\s*(?:references|参考文献)[:：]?$', head, re.I))
                    if (kind == 'title' and title_start and re.match(r'\s*(?:[.!?;,\)\]\"”]|$)', tail)
                            or kind == 'doi' and not re.match(r'[\w/-]|\.[A-Za-z0-9]', tail)):
                        break
                    start = clean.find(needle, start + 1)
                if start < 0:
                    continue  # A prefix of a longer title/DOI does not identify this paper.
                evidence = [{**anchor(c), 'quote': c['content']} for a, b, c in spans if a < right and b > left]
                matches[target['id']] = {'relation': 'cites', 'match': kind, 'evidence': evidence,
                    'version_note': 'Title match; check the cited year/version in the original.' if kind == 'title' else ''}
                break
    return list(matches.items()), sum(len(g) for g in groups)


def identity_match(seed, candidate, seed_authors, candidate_authors):
    shared = {a['name_key'] for a in seed_authors} & {a['name_key'] for a in candidate_authors} - {seed['name_key']}
    common = {'shared_paper_affiliations': sorted(set(seed['paper_affiliations']) & set(candidate['paper_affiliations'])),
              'shared_coauthors': [a['name'] for a in seed_authors if a['name_key'] in shared]}
    if seed['orcid'] and candidate['orcid']:
        match = 'shared_orcid' if seed['orcid'] == candidate['orcid'] else 'conflicting_orcid'
    elif set(seed['emails']) & set(candidate['emails']):
        match = 'shared_name_and_email'
    else:
        match = 'unresolved_same_name'
    return {'identity_match': match, **common}


class PaperRelations:
    def __init__(self, store, library):
        self.store, self.library = store, library

    def verification_context(self, space_id, question, evidence=()):
        """Source locations for named/read papers, never a substitute for reading them."""
        papers, coverage = self.sync(space_id)
        read_ids = {e.provenance.get('artifact_id') for e in evidence}
        query = normalized(question)
        documents = []
        for paper in papers:
            title = re.sub(r'\.pdf$', '', paper['title'], flags=re.I)
            key = normalized(title)
            mentioned = len(key) >= 24 and key in query
            if not mentioned and paper['id'] not in read_ids:
                continue
            chunks = self.library.chunks(space_id, paper['id'])
            if not chunks:
                continue
            aliases = [title]
            prefix = re.match(r'^([A-Z][A-Z0-9]{2,15})[:_]', title)
            if prefix:
                aliases.append(prefix[1])
            context = question + '\n' + '\n'.join(c['content'] for c in chunks if c['page'] == 1)
            for match in re.finditer(r'[（(]([A-Z][A-Z0-9]{2,15})[）)]', context):
                if normalized(context[max(0, match.start()-len(title)*2-100):match.start()]).endswith(key):
                    aliases.append(match[1])
            if mentioned and re.search(r'(?:种子(?:论文)?|seed(?:paper)?)$', query[max(0, query.index(key)-20):query.index(key)], re.I):
                aliases.extend(['种子', '种子论文', 'seed'])
            documents.append({'artifact_id': paper['id'], 'title': paper['title'], 'mentioned': mentioned,
                              'aliases': list(dict.fromkeys(aliases)), 'ref_id': anchor(chunks[0])['ref_id']})
        documents.sort(key=lambda d: not d['mentioned'])
        documents = documents[:40]
        visible = {d['artifact_id'] for d in documents}
        edges = []
        with closing(self.store._connect()) as db:
            for row in db.execute('SELECT e.* FROM paper_citation_edges e JOIN artifacts a ON a.id=e.source_id WHERE a.space_id=?', (space_id,)):
                if row['source_id'] in visible and row['target_id'] in visible:
                    detail = json.loads(row['detail'])
                    edges.append({'source_id': row['source_id'], 'target_id': row['target_id'],
                                  'evidence': [{k: v for k, v in e.items() if k != 'quote'} for e in detail['evidence']]})
        return {'documents': documents, 'citation_edges': edges, 'coverage': coverage}

    def discover_author_papers(self, space_id, seed_artifact_id, author, author_id='', query='', page=1, per_page=5):
        if not isinstance(seed_artifact_id, str) or not isinstance(author, str) or not author.strip() or len(author) > 200:
            raise ValueError('a saved seed paper and full author name are required')
        if not isinstance(author_id, str) or not isinstance(query, str) or len(query) > 300:
            raise ValueError('invalid author ID/topic')
        selected_id = author_id.removeprefix('https://openalex.org/')
        if selected_id and not re.fullmatch(r'A\d+', selected_id):
            raise ValueError('invalid OpenAlex author ID')
        if type(page) is not int or not 1 <= page <= 100 or type(per_page) is not int or not 1 <= per_page <= 10:
            raise ValueError('invalid discovery pagination')
        seed = self.library.get(space_id, seed_artifact_id)
        if seed['kind'] != 'paper' or not self.library.authorized(seed):
            raise ValueError('seed paper is not readable in the current research space')
        byline = authors(seed, self.library.chunks(space_id, seed_artifact_id))
        person = next((a for a in byline if a['name_key'] == name_key(author)), None)
        if not person:
            raise ValueError('full author name is not supported by the seed byline; do not expand initials')
        doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:)', '', str(seed['metadata'].get('doi') or seed['canonical_id']), flags=re.I).strip()
        if not re.fullmatch(r'10\.\d{4,9}/\S{1,200}', doi, re.I):
            raise ValueError('seed has no usable DOI; use search/read to verify its publication metadata')
        requests = 0
        def fetch(url):
            nonlocal requests
            requests += 1
            payload = json.loads(self.library.fetch(url, max_bytes=2_000_000)[0])
            if not isinstance(payload, dict):
                raise ValueError('invalid bibliographic response')
            return payload
        seed_url = 'https://api.openalex.org/works/https://doi.org/' + quote(doi, safe='/')
        try:
            work = fetch(seed_url)
            returned_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', str(work.get('doi') or ''), flags=re.I)
            if returned_doi.casefold() != doi.casefold():
                raise ValueError('provider work does not match the seed DOI')
            candidates = []
            for row in work.get('authorships', []):
                profile = row.get('author') or {}
                names = [str(profile.get('display_name') or ''), str(row.get('raw_author_name') or '')]
                aid = str(profile.get('id') or '').removeprefix('https://openalex.org/')
                if re.fullmatch(r'A\d+', aid) and any(name_key(n) == person['name_key'] for n in names):
                    candidates.append({'author_id': aid, 'name': profile.get('display_name'), 'orcid': profile.get('orcid'),
                                       'institutions': [i.get('display_name') for i in row.get('institutions', [])],
                                       'raw_affiliations': row.get('raw_affiliation_strings', [])})
            if selected_id and selected_id not in {a['author_id'] for a in candidates}:
                raise ValueError('author ID does not belong to the matching seed byline')
            base = {'ok': True, 'kind': 'discover_author_papers', 'seed_doi': doi, 'seed_provider_url': seed_url,
                    'seed_byline': person, 'author_candidates': candidates, 'results': [], 'sources': [],
                    'guidance': 'Provider identity/works are candidates, not original-paper proof or a complete bibliography. Prioritize relevant topics, shared coauthors and accessible PDFs, then verify identity and version from each original. A shared coauthor is only a clue. Read candidates before further broad search. If a URL fails, try another returned location for the same work; then search its exact title plus PDF or follow references in the seed. Do not count the seed or alternate versions as additional papers. Do not expand initials or infer inheritance.'}
            if not selected_id and len({a['author_id'] for a in candidates}) != 1:
                return {**base, 'network_requests': requests, 'selection_required': True}
            selected_id = selected_id or candidates[0]['author_id']
            params = {'filter': 'authorships.author.id:' + selected_id, 'per_page': per_page, 'page': page,
                      'sort': 'publication_date:desc'}
            if query.strip():
                params['search'] = query.strip()
            works_url = 'https://api.openalex.org/works?' + urlencode(params)
            payload = fetch(works_url)
            rows = payload.get('results')
            if not isinstance(rows, list) or len(rows) > per_page:
                raise ValueError('invalid author works page')
            for item in rows:
                if str(item.get('doi') or '').casefold() == 'https://doi.org/' + doi.casefold():
                    continue
                authorships = item.get('authorships') or []
                matched = [a for a in authorships if str((a.get('author') or {}).get('id', '')).removeprefix('https://openalex.org/') == selected_id]
                if not matched:
                    continue
                primary = item.get('primary_location') or {}
                locations = [item.get('best_oa_location') or {}, primary, *(item.get('locations') or [])]
                links = [loc.get('pdf_url') for loc in locations if isinstance(loc, dict)]
                links += [loc.get('landing_page_url') for loc in locations if isinstance(loc, dict)] + [item.get('doi')]
                urls = list(dict.fromkeys(canonical_url(u) for u in links if isinstance(u, str) and canonical_url(u)))[:8]
                seed_coauthors = {p['name_key'] for p in byline if p['name_key'] != person['name_key']}
                shared = [a.get('author', {}).get('display_name') for a in authorships
                          if name_key(str(a.get('author', {}).get('display_name') or '')) in seed_coauthors]
                entry = {'work_id': item.get('id'), 'title': item.get('display_name'), 'doi': item.get('doi'),
                         'publication_year': item.get('publication_year'), 'type': item.get('type'),
                         'venue': (primary.get('source') or {}).get('display_name'), 'urls': urls,
                         'shared_coauthors': shared,
                         'locations': [{'url': u, 'version': loc.get('version'), 'is_oa': loc.get('is_oa')}
                                       for loc in locations if isinstance(loc, dict)
                                       for u in [canonical_url(str(loc.get('pdf_url') or loc.get('landing_page_url') or ''))]
                                       if u and u in urls][:8],
                         'authors': [a.get('author', {}).get('display_name') for a in authorships[:30]],
                         'authors_truncated': len(authorships) > 30,
                         'identity_check': 'provider_id_match_requires_original' if any(name_key(str(a.get('raw_author_name') or (a.get('author') or {}).get('display_name') or '')) == person['name_key'] for a in matched) else 'provider_name_conflict',
                         'matched_author_affiliations': [i.get('display_name') for a in matched for i in a.get('institutions', [])]}
                base['results'].append(entry)
                for url in urls:
                    base['sources'].append({'title': str(entry['title'] or ''), 'url': url,
                        'snippet': 'OpenAlex author-work candidate; verify original authorship and publication version. ' + str(entry['publication_year']),
                        'publisher': 'OpenAlex metadata'})
            count = payload.get('meta', {}).get('count')
            base['results'].sort(key=lambda r: (r['identity_check'] == 'provider_name_conflict', -len(r['shared_coauthors'])))
            return {**base, 'author_id': selected_id, 'provider_url': works_url, 'provider_total': count,
                    'page': page, 'next_page': page + 1 if isinstance(count, int) and page * per_page < count and page < 100 else None,
                    'network_requests': requests}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            return {'ok': False, 'kind': 'discover_author_papers', 'network_requests': requests,
                    'error': {'code': 'bibliography_unavailable', 'message': str(redact(str(exc)))[:400]},
                    'guidance': 'Use ordinary search/read with the verified seed full name, affiliations and coauthors; provider failure does not prove no other papers exist.'}

    def sync(self, space_id):
        papers = [p for p in self.library.list(space_id) if p['kind'] == 'paper'
                  and p['status'] != 'unreadable' and self.library.authorized(p)]
        papers.sort(key=lambda p: p['id'])
        chunks = {p['id']: self.library.chunks(space_id, p['id']) for p in papers}
        fingerprint = hashlib.sha256(json.dumps([6, [(p['id'], p['title'], p['metadata'], chunks[p['id']])
                                     for p in papers]], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with closing(self.store._connect()) as db, db:
            old = db.execute('SELECT * FROM paper_relation_state WHERE space_id=?', (space_id,)).fetchone()
            if old and old['fingerprint'] == fingerprint:
                return papers, json.loads(old['coverage'])
            db.execute('DELETE FROM paper_authorships WHERE artifact_id IN (SELECT id FROM artifacts WHERE space_id=?)', (space_id,))
            db.execute('DELETE FROM paper_citation_edges WHERE source_id IN (SELECT id FROM artifacts WHERE space_id=?)', (space_id,))
            coverage = {'papers': len(papers), 'papers_with_authors': 0, 'papers_with_references': 0, 'citation_edges': 0}
            for paper in papers:
                people = authors(paper, chunks[paper['id']])
                coverage['papers_with_authors'] += bool(people)
                for person in people:
                    db.execute('INSERT OR REPLACE INTO paper_authorships VALUES(?,?,?)',
                               (paper['id'], person['name_key'], json.dumps(person, ensure_ascii=False)))
                edges, references = reference_matches(chunks[paper['id']], [p for p in papers if p['id'] != paper['id']])
                coverage['papers_with_references'] += bool(references)
                coverage['citation_edges'] += len(edges)
                for target, detail in edges:
                    db.execute('INSERT INTO paper_citation_edges VALUES(?,?,?)',
                               (paper['id'], target, json.dumps(detail, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO paper_relation_state VALUES(?,?,?)',
                       (space_id, fingerprint, json.dumps(coverage)))
        return papers, coverage

    def lookup(self, space_id, mode, artifact_ids=None, author='', query='', offset=0, limit=8):
        if mode not in {'authors', 'author_papers', 'references', 'cited_by', 'shared_references'}:
            raise ValueError('invalid paper lookup mode')
        if artifact_ids is not None and (not isinstance(artifact_ids, list) or len(artifact_ids) > 20
                                        or any(not isinstance(x, str) for x in artifact_ids)):
            raise ValueError('invalid artifact_ids')
        if not isinstance(author, str) or len(author) > 200 or not isinstance(query, str) or len(query) > 500:
            raise ValueError('invalid author/query')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError('invalid pagination')
        ids = list(dict.fromkeys(artifact_ids or []))
        if mode == 'author_papers' and (not author.strip() or len(ids) > 1):
            raise ValueError('author_papers requires a full author name and at most one seed artifact')
        if mode in {'references', 'cited_by', 'shared_references'} and not ids:
            raise ValueError('citation lookup requires artifact_ids')
        if mode == 'shared_references' and len(ids) < 2:
            raise ValueError('shared_references requires at least two distinct papers')
        papers, coverage = self.sync(space_id)
        visible = {p['id']: p for p in papers}
        if any(i not in visible for i in ids):
            raise ValueError('paper is unavailable or outside the current research space')
        people, edges = {}, []
        with closing(self.store._connect()) as db:
            for row in db.execute('SELECT p.* FROM paper_authorships p JOIN artifacts a ON a.id=p.artifact_id WHERE a.space_id=?', (space_id,)):
                if row['artifact_id'] in visible:
                    people.setdefault(row['artifact_id'], []).append(json.loads(row['detail']))
            for row in db.execute('SELECT e.* FROM paper_citation_edges e JOIN artifacts a ON a.id=e.source_id WHERE a.space_id=?', (space_id,)):
                if row['source_id'] in visible and row['target_id'] in visible:
                    edges.append({'source_id': row['source_id'], 'target_id': row['target_id'], **json.loads(row['detail'])})
        for byline in people.values():
            byline.sort(key=lambda a: a['position'])
        def summary(item_id):
            p = visible[item_id]
            return {'artifact_id': item_id, 'title': p['title'], 'url': p['url'],
                    'year': p['metadata'].get('year'), 'doi': p['metadata'].get('doi'),
                    'authors': people.get(item_id, [])}
        results = []
        if mode in {'authors', 'author_papers'}:
            seed_people = people.get(ids[0], []) if ids and mode == 'author_papers' else []
            seed = next((a for a in seed_people if a['name_key'] == name_key(author)), None)
            if ids and mode == 'author_papers' and not seed:
                raise ValueError('the requested author is not supported by the seed byline')
            for p in papers:
                if mode == 'authors' and ids and p['id'] not in ids:
                    continue
                selected = [a for a in people.get(p['id'], []) if not author or a['name_key'] == name_key(author)]
                if author and not selected:
                    continue
                if query and normalized(query) not in normalized(p['title'] + ' ' + ' '.join(a['name'] for a in selected)):
                    continue
                result = summary(p['id'])
                if mode == 'author_papers':
                    result['author'] = selected[0]
                    result.update(identity_match(seed, selected[0], seed_people, people[p['id']]) if seed else {'identity_match': 'unresolved_same_name'})
                    if ids and p['id'] == ids[0]:
                        result['identity_match'] = 'seed_record'
                results.append(result)
        else:
            selected = [e for e in edges if (e['target_id'] if mode == 'cited_by' else e['source_id']) in ids]
            if mode == 'shared_references':
                shared = {e['target_id'] for e in selected if {x['source_id'] for x in selected if x['target_id'] == e['target_id']} == set(ids)}
                selected = [e for e in selected if e['target_id'] in shared]
            for edge in selected:
                result = {**edge, 'source_title': visible[edge['source_id']]['title'], 'target': summary(edge['target_id'])}
                if not query or normalized(query) in normalized(result['source_title'] + ' ' + result['target']['title']):
                    results.append(result)
        results.sort(key=lambda r: (r.get('title', r.get('source_title', '')), r.get('artifact_id', r.get('target_id', ''))))
        page = results[offset:offset + limit]
        return {'ok': True, 'kind': 'lookup_papers', 'mode': mode, 'results': page, 'total': len(results),
                'next_offset': offset + limit if offset + limit < len(results) else None, 'coverage': coverage,
                'guidance': 'Read evidence ref_ids with read_evidence before citing facts. Paper affiliations and coauthors are identity clues, not proof that names identify one person. Initials are never expanded. Citation edges cover only recognizable references to saved papers; title matches may differ in version. Missing results do not prove absence. A citation does not establish method/code inheritance.'}
