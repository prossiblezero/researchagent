"""Encoder content and cache isolation without loading real model weights."""
import hashlib
import os
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_agent import retrieval as r


class ModelIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.model = self.root / 'encoder'
        self.model.mkdir()
        (self.model / 'researchagent-revision.txt').write_text(r.REVISION, encoding='utf-8')
        (self.model / 'config.json').write_text('{"hidden_size":2}', encoding='utf-8')
        (self.model / 'tokenizer.json').write_text('{"vocab":{"a":1}}', encoding='utf-8')
        (self.model / 'model.safetensors').write_bytes(b'AAAA')
        self.addCleanup(r._model_file_hash.cache_clear)
        r._model_file_hash.cache_clear()
        self.enterContext(patch.object(r, '_MODELS', {}))
        self.enterContext(patch.object(r, '_EMBEDDINGS', OrderedDict()))
        self.enterContext(patch.dict(os.environ, {'BGE_DEVICE': 'cpu'}))

    def replace_preserving_mtime(self, name, content):
        target = self.model / name
        before = target.stat()
        replacement = self.model / 'replacement.tmp'
        replacement.write_bytes(content)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(target)
        self.assertEqual(target.stat().st_size, before.st_size)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_same_size_and_timestamp_replacements_change_content_identity(self):
        previous = r.model_identity(self.model)
        for name, value in [('model.safetensors', b'BBBB'),
                            ('tokenizer.json', b'{"vocab":{"b":1}}'),
                            ('config.json', b'{"hidden_size":3}')]:
            with self.subTest(file=name):
                self.replace_preserving_mtime(name, value)
                current = r.model_identity(self.model)
                self.assertNotEqual(previous['manifest_hash'], current['manifest_hash'])
                self.assertNotEqual(previous['fingerprint'], current['fingerprint'])
                self.assertEqual(current['revision'], r.REVISION)
                previous = current
        self.assertEqual(previous['config_hash'], hashlib.sha256(b'{"hidden_size":3}').hexdigest())

    def test_unchanged_weight_is_not_reread_and_timestamp_only_change_is_stable(self):
        weight = self.model / 'model.safetensors'
        weight.write_bytes(b'a' * (2 * 1024 * 1024 + 17))
        opened = []
        original = Path.open
        def open_file(path, *args, **kwargs):
            if path == weight:
                opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            first = r.model_identity(self.model)
            self.assertEqual(r.model_identity(self.model), first)
            self.assertEqual(len(opened), 1)
            info = weight.stat()
            os.utime(weight, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
            self.assertEqual(r.model_identity(self.model), first)
            self.assertEqual(len(opened), 2)

    def fake_runtime(self, mutate_during_load=False):
        class Vector(list):
            def tolist(self): return list(self)
        def load(path, **kwargs):
            value = sum((Path(path) / 'model.safetensors').read_bytes())
            if kwargs['device'] != 'cpu': value += 1000
            if mutate_during_load:
                self.replace_preserving_mtime('model.safetensors', b'ZZZZ')
            return SimpleNamespace(encode=Mock(side_effect=lambda texts, **kw: [Vector([value, len(text)]) for text in texts]))
        factory = Mock(side_effect=load)
        client = SimpleNamespace(get_or_create_collection=Mock(return_value=Mock()))
        self.enterContext(patch.dict('sys.modules', {
            'sentence_transformers': SimpleNamespace(SentenceTransformer=factory),
            'chromadb': SimpleNamespace(PersistentClient=Mock(return_value=client)),
            'chromadb.config': SimpleNamespace(Settings=Mock()),
            'torch': SimpleNamespace(set_num_threads=Mock()),
        }))
        return factory

    def test_loaded_models_and_embeddings_are_isolated_after_same_path_replacement(self):
        factory = self.fake_runtime()
        first = r.DenseIndex(self.root, model_path=self.model)
        same = r.DenseIndex(self.root, model_path=self.model)
        self.assertIs(first.model, same.model)
        self.assertEqual(first.encode(['shared text']), [[260, 11]])
        self.assertEqual(same.encode(['shared text']), [[260, 11]])
        first.model.encode.assert_called_once()
        self.replace_preserving_mtime('model.safetensors', b'BBBB')
        changed = r.DenseIndex(self.root, model_path=self.model)
        self.assertIsNot(changed.model, first.model)
        self.assertEqual(changed.encode(['shared text']), [[264, 11]])
        self.assertEqual(first.encode(['shared text']), [[260, 11]])
        self.assertEqual(factory.call_count, 2)
        changed.model.encode.assert_called_once()

    def test_embedding_cache_separates_encoder_devices(self):
        factory = self.fake_runtime()
        cpu = r.DenseIndex(self.root, model_path=self.model)
        self.assertEqual(cpu.encode(['same']), [[260, 4]])
        with patch.dict(os.environ, {'BGE_DEVICE': 'cuda:0'}):
            other_device = r.DenseIndex(self.root, model_path=self.model)
        self.assertEqual(other_device.encode(['same']), [[1260, 4]])
        self.assertEqual(cpu.encode(['same']), [[260, 4]])
        self.assertEqual(factory.call_count, 2)

    def test_encoder_changed_during_loading_is_not_cached(self):
        self.fake_runtime(mutate_during_load=True)
        with self.assertRaisesRegex(ValueError, 'changed while loading'):
            r.DenseIndex(self.root, model_path=self.model)
        self.assertFalse(r._MODELS)


if __name__ == '__main__':
    unittest.main()
