"""Bounded original-passages loading must not rehash the whole library per hit."""
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from research_agent.library import Library
from research_agent.retrieval import Retriever, digest
from research_agent.workbench_store import WorkbenchStore


class EvidenceBatchReadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'RETRIEVAL_MODE': 'lexical'})
        env.start(); self.addCleanup(env.stop)
        self.store = WorkbenchStore(self.root / 'state.db')
        self.sid = self.store.save_space({'name': 'batch originals', 'download_root': str(self.root)})['id']
        self.library = Library(self.store)
        self.retriever = Retriever(self.store, self.library)

    def document(self, title, text=None, sid=None):
        return self.library.save(sid or self.sid, {
            'kind': 'document', 'title': title, 'url': '', 'canonical_id': title,
            'metadata': {}, 'data': None, 'warnings': [], 'boundary': 'local test fixture',
            'chunks': [{'text': text or 'alpha original passage with sufficient detail for indexing.',
                        'page': 2, 'section': 'methods', 'line_start': 1, 'line_end': 4}],
        })[0]

    def collect(self, **kwargs):
        seen = []
        def rank(query, passages):
            seen.extend(passages)
            return {'status': 'ok', 'ranking': [p['id'] for p in passages][:10]}
        result = self.retriever.retrieve(self.sid, 'alpha', evidence_ranker=rank, **kwargs)
        return result, seen

    def test_forty_parents_are_loaded_in_one_batch_and_match_canonical_reads(self):
        for index in range(40):
            text = 'alpha ' + ('context ' * 240 if index == 0 else 'ordinary source detail ') + f'complete original {index}.'
            self.document(f'document {index}', text)
        with patch.object(self.library, 'authorized', wraps=self.library.authorized) as authorize:
            result, passages = self.collect()
            checks = authorize.call_count
        self.assertEqual(len(passages), 40)
        self.assertEqual(result['evidence_selection']['candidate_passages'], 40)
        self.assertTrue(all(hit['preview_only'] for hit in result['results']))
        self.assertEqual([h['ref_id'] for h in result['results']], [p['id'] for p in passages][:len(result['results'])])
        for passage in passages:
            original = self.retriever.read(self.sid, passage['id'], adjacent=0)
            self.assertEqual(passage, {'id': passage['id'], **{
                key: original[key] for key in ('title', 'section', 'content', 'content_hash')}})
        self.assertTrue(any(len(p['content']) > 1800 for p in passages), 'full parents, not embedding windows')
        self.assertLessEqual(checks, 80, 'each source may be checked during sync and once for the whole candidate batch')

    def test_next_retrieval_rechecks_deleted_changed_and_modified_file_sources(self):
        self.document('keep')
        changed = self.document('changed', 'alpha old original content which must not survive a new retrieval.')
        deleted = self.document('deleted')
        altered_file = self.document('altered file')
        file = self.root / 'original.txt'; file.write_text('original file fixture', encoding='utf-8')
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE artifacts SET original_path=?,digest=? WHERE id=?',
                       (str(file), digest(file.read_text(encoding='utf-8')), altered_file['id']))
        self.assertEqual(len(self.collect()[1]), 4)
        new_text = 'alpha fresh original content from the updated source, never the old cached passage.'
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE document_chunks SET content=? WHERE artifact_id=?', (new_text, changed['id']))
            db.execute('DELETE FROM document_chunks WHERE artifact_id=?', (deleted['id'],))
        file.write_text('modified file fixture', encoding='utf-8')
        passages = self.collect()[1]
        self.assertEqual({p['title'] for p in passages}, {'keep', 'changed'})
        self.assertEqual(next(p['content'] for p in passages if p['title'] == 'changed'), new_text)

    def test_change_between_index_sync_and_batch_load_rejects_stale_segment(self):
        self.document('keep')
        stale = self.document('stale')
        sync = self.retriever.sync
        def change_after_sync(*args, **kwargs):
            rows = sync(*args, **kwargs)
            with closing(self.store._connect()) as db, db:
                db.execute('UPDATE document_chunks SET content=? WHERE artifact_id=?',
                           ('alpha changed after index snapshot; this text needs a new retrieval.', stale['id']))
            return rows
        with patch.object(self.retriever, 'sync', side_effect=change_after_sync):
            passages = self.collect()[1]
        self.assertEqual([p['title'] for p in passages], ['keep'])

    def test_artifact_space_and_noncurrent_scope_boundaries_remain_enforced(self):
        target = self.document('selected')
        self.document('excluded by artifact filter')
        other = self.store.save_space({'name': 'other research area'})['id']
        self.document('private other area', sid=other)
        passages = self.collect(artifact_ids=[target['id']])[1]
        self.assertEqual([p['title'] for p in passages], ['selected'])
        rank = Mock(side_effect=AssertionError('scope must not invoke evidence reranking'))
        self.retriever.retrieve(self.sid, 'alpha', scope='workspace', evidence_ranker=rank)
        self.retriever.retrieve(self.sid, 'alpha', corpus='memory', evidence_ranker=rank)
        rank.assert_not_called()


if __name__ == '__main__':
    unittest.main()
