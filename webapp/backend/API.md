# Defeature Backend API Notes

## Batch fillet-component deletion failures

`POST /api/sessions/{session_id}/fillet-components/delete-batch` returns HTTP `422` when an NX deletion attempt was made but did not produce a validated Candidate. The response body retains the structured service result under `detail`:

```json
{
  "detail": {
    "schema": "fillet_components_delete_result_v1",
    "status": "failed",
    "source_revision": "r0",
    "candidate_revision": null,
    "component_ids": ["fc-001", "fc-002"],
    "strategy": "whole-component-face-edge-blend",
    "failure_category": "geometry",
    "error": "...",
    "rollback": true,
    "allowed_next_strategies": []
  }
}
```

The `failure_category` is tool-facing and controls retry behavior:

- `geometry`: the caller may retry a smaller component subset according to its deterministic bisection policy.
- `infrastructure`: stop without bisection or geometry-strategy fallback.
- `validation`: stop; the STEP/NX report/OCC result was not safe to publish.

All failed categories leave the Accepted Revision unchanged and publish no Candidate.

The DSH tool adapter preserves non-string FastAPI `detail` values by JSON-encoding that object as the thrown error message. Tool callers can therefore parse the message and inspect `failure_category`; this behavior is covered by `dsh-plugins/defeature-ui/test/tools.test.mjs`.
