# Persistent NX2406 Runtime

## Scope

The development preset can run long-tail NXOpen Python inside one persistent `run_journal.exe` process. The production `defeature-agent` remains restricted to the mature allowlisted fillet tools and does not register dynamic execution.

The persistent worker journal is `cad/nx_persistent_worker.py`.

## Lifecycle and state

1. Resolve the DSH `session_id` to its backend Job and current Accepted revision.
2. Allocate a fresh worker-incarnation directory and start `cad/nx_persistent_worker.py` once.
3. Import the Accepted STEP once, export NX's normalized STEP, re-import that normalized STEP, and use only the re-imported part as the runtime model. Publish `heartbeat.json` with PID, status and `runtime_state=0`.
4. Submit atomic request JSON files through `requests/`; the Journal claims them into `running/` and writes terminal records into `results/`.
5. `read` must preserve the model, `preview` always undoes, and a successful `commit` retains the change and advances runtime state exactly once.
6. A timeout is ambiguous, not cancellation. Query the same `operation_id`; never retry a mutation with another ID.
7. Publication uses a fixed worker-owned STEP export action to a backend-owned path. The backend checks the non-empty file, imports it with OCC, builds the face mesh and creates exactly one Candidate.
8. Candidate acceptance or rejection invalidates and stops the current runtime. The next runtime starts from the then-current Accepted STEP at state 0.

## Backend API

All endpoints are session-keyed; callers do not supply Job IDs, revision IDs, paths, or worker IDs.

- `GET /api/sessions/{session_id}/nx-runtime/status`
- `POST /api/sessions/{session_id}/nx-runtime/execute`
- `GET /api/sessions/{session_id}/nx-runtime/operations/{operation_id}`
- `POST /api/sessions/{session_id}/nx-runtime/reset`
- `POST /api/sessions/{session_id}/nx-runtime/publish-candidate`

Execute request:

```json
{
  "operation_id": "op-example-001",
  "base_state": 0,
  "mode": "read",
  "code": "result = {'body_count': len(list(workPart.Bodies))}",
  "preview_operation_id": null
}
```

The execution namespace provides `NXOpen`, `session`, `workPart`, `displayPart`, and `ctx`. It is high privilege and is not an OS sandbox.

## Transaction behavior

- Each operation gets a visible NX Undo mark.
- Preview and failed execution invoke `UndoToMark`, update, then compare the restored model signature.
- Read compares pre/post signatures and rejects a mutating read.
- Restoration mismatch quarantines the worker.
- The signature covers work/display part identity, body identity and topology, feature count, axis-aligned extents, and an edge-vertex checksum. It improves rollback detection but does not constitute a formal BREP equivalence proof.
- Undo marks are deleted after operation finalization to avoid unbounded accumulation.

## Agent separation

The Host Tool plugin accepts `enableDynamicRuntime`:

- `defeature-agent-dev`: sets it to `true` and receives status, execute, reconcile, reset, and publish tools.
- `defeature-agent`: omits it and therefore receives no arbitrary NXOpen tool.

The CAD workbench shows persistent runtime status only for the development preset.

## Operational cautions

- Queue directories are unique per coordinator-created worker incarnation.
- If graceful stop times out, process ownership is retained and restart is prohibited until the process is reconciled.
- Backend process restart currently discards in-memory coordinator bindings. Existing NX processes are not adopted; operational deployment should stop them before restarting the backend.
- Dynamic code can access the NX process capabilities and should be used only in the development environment.

## Startup normalization

The startup model flow is deliberately a one-way NX round trip:

```text
Accepted STEP
→ import into NX
→ export NX-normalized STEP
→ close the original NX part
→ re-import the normalized STEP into NX
→ use the re-imported part for runtime analysis and CAD operations
```

Startup requires the NX export to succeed, the normalized file to exist and be non-empty, and NX to successfully re-import it as the worker's current model. The original and normalized files are not required to have identical mass properties, bounding boxes, face classifications, or geometry signatures; NX export may approximate or otherwise normalize the representation. The heartbeat reports counts from the re-imported normalized model only.

This startup policy is separate from transaction rollback validation. Undo still requires exact identity, topology, feature, boundary, and vertex-checksum restoration; only finite mass-property measurements use the configured absolute and relative tolerances.
