"""Run only inside the native experiment sandbox against the staged A-MEM adapter."""
from pathlib import Path
import contextlib
import io
import json
import sys
import run_baseline as base

root = Path.cwd().resolve()
probe = root / "cache-contract-probe"
probe.mkdir(exist_ok=False)
base.ROOT = probe
initial = base.bind_cache(13, "sudocode-luna", 10)
assert base.bind_cache(13, "sudocode-luna", 10) == initial
checks = ["new cache bound", "matching cache accepted"]

def rejected(name, call):
    try:
        call()
    except RuntimeError as exc:
        assert "cache" in str(exc).lower() or "model identity" in str(exc), str(exc)
    else:
        raise AssertionError(name + " unexpectedly accepted")
    checks.append(name)

for name, values in [("seed mismatch", (14, "sudocode-luna", 10)),
                     ("model mismatch", (13, "another-model", 10)),
                     ("parameter mismatch", (13, "sudocode-luna", 5))]:
    rejected(name, lambda values=values: base.bind_cache(*values))
for name in ("METHOD", "TASKS_PATH", "CORPUS_PATH", "__file__", "UPSTREAM", "MODEL_PATH"):
    original = getattr(base, name)
    if name == "METHOD":
        changed = "another-method"
    elif name in ("UPSTREAM", "MODEL_PATH"):
        changed = probe / name
        changed.mkdir()
        for file in (["memory.py"] if name == "UPSTREAM" else ["modules.json", "config.json", "model.safetensors", "tokenizer.json"]):
            (changed / file).write_text("changed", encoding="utf-8")
    else:
        changed = probe / name
        changed.write_text("changed", encoding="utf-8")
    setattr(base, name, changed)
    rejected(name + " mismatch", lambda: base.bind_cache(13, "sudocode-luna", 10))
    setattr(base, name, original)
base.ROOT = probe / "unbound"
(base.ROOT / "results/baseline-state").mkdir(parents=True)
rejected("unbound state rejected", lambda: base.bind_cache(13, "sudocode-luna", 10))
controller = base.StdioController("sudocode-luna", 13, "probe")
reply = {"id": "baseline-13-probe-000000", "model_id": "another-model", "content": "wrong model"}
stdin = sys.stdin
try:
    sys.stdin = io.StringIO(json.dumps(reply) + "\n")
    with contextlib.redirect_stdout(io.StringIO()):
        rejected("host model identity mismatch", lambda: controller.get_completion("probe"))
finally:
    sys.stdin = stdin
print(json.dumps({"passed": True, "checks": checks, "model_calls": 0}), flush=True)
