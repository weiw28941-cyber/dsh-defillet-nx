# Controlled CAD Tools for Persistent NX

These tools are registered only when the DSH plugin option `enableDynamicRuntime` is true (the `defeature-agent-dev` preset). The production `defeature-agent` allowlist is unchanged.

## Lifecycle

1. Call `defeature_reanalyze_current_state` to inspect the live NX part and issue state-bound references such as `rs000003-fc-0007`.
2. Optionally call `defeature_get_component_topology` and `defeature_segment_fillet_component`.
3. Call `defeature_try_delete_component` or `defeature_try_delete_segment`.
4. `DeleteFaceBuilder.Commit()` without an exception is success and is retained. An exception is failure and the Worker performs Undo plus restoration verification.
5. Every retained deletion advances `runtime_state` once and invalidates every component, segment, and face reference. Reanalyze before choosing another action.
6. Call `defeature_finish_and_publish` once after the in-memory sequence is complete. It performs one STEP export, OCC import/mesh, and creates one Candidate.

## Tools

| Tool | Mutates NX | Purpose |
|---|---:|---|
| `defeature_reanalyze_current_state` | No | Detect current curved-face components and issue state-bound references. |
| `defeature_get_component_topology` | No | Return face references, surface types, branch/end faces, and loop status. |
| `defeature_segment_fillet_component` | No | Create state-bound segments using `topology_branch`, `simple_chains`, `surface_type`, or `bisection`. |
| `defeature_try_delete_component` | Commit or rollback | Try an allowlisted method on a whole component. |
| `defeature_try_delete_segment` | Commit or rollback | Try an allowlisted method on a generated segment. |
| `defeature_finish_and_publish` | Export only | Export and validate the retained state and publish one Candidate. |

Deletion methods are `face_edge_blend`, `blend`, and `generic_face`. FaceEdgeBlend preference is `cliff` or `notch`. `DeletePartialBlend` is hard-disabled (`False`) and is not accepted by the Tool schemas. Local deletion is available only through a generated, state-bound segment.

## Reference safety

References encode the runtime state. A successful commit makes them stale. The backend also binds its catalog to the persistent Worker incarnation. Stale or unknown references are rejected; no nearest-face, traversal-order, or raw-Tag fallback is allowed.

Segmentation is deterministic for a given state and method. Segment IDs include a method-derived key so invoking a different segmentation method cannot silently retarget an earlier segment ID.

## Failure and timeout semantics

A normal NX Commit exception returns `commit_succeeded=false`, retains no change, and reports whether rollback succeeded. If rollback fails, reset is required. A timeout is ambiguous, not a failed deletion; the operation must be reconciled and must not be retried under a new operation ID.

The current success criterion is intentionally narrow: only whether `DeleteFaceBuilder.Commit()` throws. Later geometry or OCC validation is deferred to final publication.
