"""Native-only probe: frozen snapshot files must not freeze mutable siblings."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import run_baseline as baseline

parser = argparse.ArgumentParser()
parser.add_argument("--expect-denied", action="store_true")
args = parser.parse_args()
folder = Path.cwd() / "results/baseline-state"
frozen = [folder / "conv-26.json", folder / "conv-26.slot0.npy"]
before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}
state = SimpleNamespace(evo_cnt=0, memories={}, retriever=SimpleNamespace(
    embeddings=np.zeros((0, 384)), corpus=[], document_ids=[]))
probe = folder / "conv-30-preflight.json"
assert not probe.exists()
try:
    baseline.save_state(probe, state, "conv-30-preflight", 0)
except PermissionError:
    assert args.expect_denied, "Mutable checkpoint sibling unexpectedly denied"
    outcome = "directory protection blocks checkpoint write"
else:
    assert not args.expect_denied, "Whole-directory protection unexpectedly allowed write"
    baseline.save_state(probe, state, "conv-30-preflight", 1)
    result = json.loads(probe.read_text())
    assert result["turn_index"] == 1 and result["embeddings_file"].endswith("slot1.npy")
    assert np.load(folder / result["embeddings_file"], allow_pickle=False).shape == (0, 384)
    outcome = "file snapshots protected; mutable sibling writes and atomic replacement succeed"
assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in frozen}
print(json.dumps({"passed": True, "outcome": outcome, "frozen_snapshots_unchanged": True, "model_calls": 0}))
