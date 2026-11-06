# NX Persistent Operation Benchmark

This benchmark measures deletion attempts while keeping one NX2406 `run_journal.exe` process and one imported STEP model alive.

## Run

```powershell
python `
  scripts\benchmark_nx_persistent_operations.py `
  --input local-artifacts\input.stp `
  --output-dir benchmark-results `
  --repetitions 10 `
  --three-plus-count 3 `
  --seed 2406
```

Each run creates an isolated timestamped directory containing:

- the copied STEP input;
- the persistent worker queue and log;
- `benchmark.json` with every attempt and aggregated latency statistics.

## Cases

The runner inventories the current NX faces and exercises:

- one curved face with `DeleteFaceBuilder.Type=FaceEdgeBlend`;
- two curved faces in one FaceEdgeBlend operation;
- an explicit three-or-more curved-face operation;
- one connected curved-face component;
- an explicit operation combining three-or-more distinct curved components;
- one planar face with generic `DeleteFaceBuilder.Type=Face`;
- two planar faces in one generic Face operation;
- an explicit three-or-more planar-face operation;
- an expected failure using FaceEdgeBlend on a planar face;
- an expected failure with a nonexistent face Tag.

`--repetitions 10` means ten **distinct sampled face groups per operation type**, not ten repetitions of the same tags. Sampling is pseudo-random without duplicate groups within an operation type and is reproducible through `--seed` (default `2406`). Face sets are not automatically asserted to be semantic design features.

## Isolation and interpretation

Every deletion case runs in `preview` mode. NX attempts the operation, updates the part, then the persistent Worker undoes it and verifies the baseline signature. Therefore all cases use the same initial topology and are directly comparable without repeatedly starting NX.

A successful preview has:

```text
state = rolled_back
error_code = null
success = true
```

A failed deletion may also have `state=rolled_back`, because rollback succeeded. Distinguish the operation outcome using `success` and `error_code`, not the terminal state alone. A failed case can be faster than a successful one when NX rejects the selection early, so success and failure latency should be analyzed separately.

The primary metric is `deletion_seconds`. It measures the Worker's `execute_seconds` around current-face lookup, DeleteFaceBuilder construction/configuration, rule selection, `Commit()` attempt, and builder destruction. The same interval is recorded whether the deletion succeeds or raises an NX error.

It deliberately excludes queue polling, model signatures, Undo, restoration verification, and result transport. `end_to_end_seconds` and the full `worker_timings` object remain diagnostic fields only; they must not be used as deletion speed.

`startup_seconds` and `discovery_seconds` are also reported separately and are not part of deletion time.

## Output schema

`benchmark.json` contains:

- Worker build and PID;
- startup and discovery times;
- baseline mass/topology/bounding-box signature;
- face inventory by type;
- one row per attempt with selected tags, success/failure, error, primary `deletion_seconds`, and diagnostic `end_to_end_seconds`;
- Worker phase timings for baseline signature, Undo mark, code execution, NX update, post-signature, rollback and total operation time;
- per-case success/failure counts and deletion-time mean, median, minimum, maximum and p95 (plus diagnostic mean end-to-end time).

The output is suitable as initial supervised/RL transition data, but a production dataset should also include geometry descriptors, component identities, before/after candidate counts and final OCC validity.
