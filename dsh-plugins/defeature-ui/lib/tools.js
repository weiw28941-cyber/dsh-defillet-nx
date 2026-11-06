import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = '@defeature/dsh-ui/tools'
export const inject = ['tools', 'attachments', 'llm']

const imageValueSchema = {
  type: 'object',
  additionalProperties: false,
  required: true,
  properties: {
    attachmentId: { type: 'string', required: true },
    mediaType: { type: 'string', enum: ['image/png'], required: true },
    bytes: { type: 'integer', required: true },
    width: { type: 'integer', required: true },
    height: { type: 'integer', required: true },
    name: { type: 'string' },
  },
}

const jsonOutput = {
  schema: { type: 'object', additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
}

const visualAnalysisOutput = {
  schema: {
    type: 'object',
    additionalProperties: true,
    properties: { image: imageValueSchema },
  },
  render: (_args, value) => [{
    type: 'text',
    text: JSON.stringify({ ...value, image: undefined }, null, 2),
  }, {
    type: 'image',
    attachment: value.image,
  }],
}

function sessionId(exec) {
  const value = exec.agent?.id
  if (!value) throw new Error('Defeature tools require a live DSH agent session')
  return encodeURIComponent(String(value))
}

async function assertImageCapableRoute(ctx, exec) {
  const routed = exec.agent?.session?.requestHeader?.()?.config
  const provider = routed?.provider || exec.agent?.options?.provider
  const model = routed?.model || exec.agent?.options?.model
  if (!provider || !model) throw new Error('fillet visual analysis requires a resolved image-capable model route')
  const active = await ctx.llm.resolveModelInfo(provider, model, exec.signal)
  if (!active.inputModalities?.includes('image')) {
    throw new Error(`model "${model}" does not declare image input; switch to an image-capable model`)
  }
}

function backendErrorMessage(status, payload) {
  const detail = payload && typeof payload === 'object' && 'detail' in payload
    ? payload.detail
    : payload
  if (detail !== undefined && detail !== null && detail !== '') {
    if (typeof detail === 'string') return detail
    try {
      const json = JSON.stringify(detail)
      if (json !== undefined) return json
    } catch {
      // Fall through to a stable HTTP error rather than masking the backend failure.
    }
  }
  return `Defeature backend returned HTTP ${status}`
}

async function callBackend(baseUrl, path, init = {}, signal) {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    signal,
    headers: {
      accept: 'application/json',
      ...(init.body === undefined ? {} : { 'content-type': 'application/json' }),
      ...(init.headers || {}),
    },
  })
  const text = await response.text()
  let payload
  try { payload = text ? JSON.parse(text) : null } catch {
    throw new Error(`Defeature backend returned invalid JSON (HTTP ${response.status})`)
  }
  if (!response.ok) {
    throw new Error(backendErrorMessage(response.status, payload))
  }
  return payload
}

export function apply(ctx, config = {}) {
  const baseUrl = String(config.baseUrl || process.env.DSH_WEB_URL || 'http://127.0.0.1:3080')
    .replace(/\/$/, '') + '/defeature/api'
  ctx.effect(() => {
    const disposers = []
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_get_context',
      description: 'Get the CAD job and accepted/candidate revision state bound to the current DSH session. Takes no job ID or file path.',
      parameters: {},
      output: jsonOutput,
      isConcurrencySafe: () => true,
      async execute(_args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/job`, {}, exec.signal)
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_rollback_accepted_revision',
      description: 'Roll the current session Accepted Revision back to a specified strict ancestor. Rejects pending Candidates, unknown revisions, and non-ancestor targets.',
      parameters: {
        target_revision: { type: 'string', required: true, description: 'Ancestor revision ID such as r0.' },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/revisions/rollback`,
          { method: 'POST', body: JSON.stringify({ target_revision: args.target_revision }) },
          exec.signal,
        )
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_get_selection',
      description: 'Get the faces currently selected with the mouse in the CAD workbench for this DSH session and accepted revision.',
      parameters: {},
      output: jsonOutput,
      isConcurrencySafe: () => true,
      async execute(_args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/selection`, {}, exec.signal)
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_detect_fillet_components',
      description: 'Use Siemens NX on the current session accepted revision to detect curved fillet candidates and partition them into shared-edge connected components.',
      parameters: {
        force: {
          type: 'boolean',
          description: 'Ignore the cached report and run NX again. Defaults to false.',
        },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const force = args.force === true ? 'true' : 'false'
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/fillet-components/detect?force=${force}`,
          { method: 'POST' },
          exec.signal,
        )
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_analyze_fillet_components',
      description: 'Render every detected fillet component in a distinct color across isometric, front, right, and top views. Use the image to reason about each component shape and plausible removal method.',
      parameters: {},
      output: visualAnalysisOutput,
      isConcurrencySafe: () => false,
      async execute(_args, exec) {
        await assertImageCapableRoute(ctx, exec)
        const sid = sessionId(exec)
        const value = await callBackend(
          baseUrl,
          `/sessions/${sid}/fillet-components/analyze`,
          { method: 'POST' },
          exec.signal,
        )
        const imagePath = `/sessions/${sid}/fillet-components/analysis-image/${encodeURIComponent(value.job_id)}/${encodeURIComponent(value.revision_id)}`
        const response = await fetch(`${baseUrl}${imagePath}`, {
          signal: exec.signal,
          headers: { accept: 'image/png' },
        })
        if (!response.ok) throw new Error(`Defeature analysis image returned HTTP ${response.status}`)
        const data = new Uint8Array(await response.arrayBuffer())
        const image = await ctx.attachments.saveImage({
          data,
          mediaType: 'image/png',
          name: value.image_name || 'fillet-components.png',
        })
        return { ...value, image }
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_delete_fillet_component',
      description: 'Delete exactly one validated fillet component from the current accepted revision with an allowlisted NX strategy and publish a Candidate revision on success.',
      parameters: {
        component_id: { type: 'string', required: true, description: 'Component ID returned for the current accepted revision.' },
        strategy: { type: 'string', enum: ['whole-component-face-edge-blend'], description: 'Allowlisted deletion strategy.' },
        preference: { type: 'string', enum: ['cliff', 'notch'], description: 'NX FaceEdgeBlend preference. Defaults to cliff.' },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/fillet-components/delete`,
          {
            method: 'POST',
            body: JSON.stringify({
              component_id: args.component_id,
              strategy: args.strategy || 'whole-component-face-edge-blend',
              preference: args.preference || 'cliff',
            }),
          },
          exec.signal,
        )
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_remove_all_fillets',
      description: 'Remove every deletable fillet component in one tool call and one NX process: import once, detect and bisect with in-memory undo, export once, validate once with OCC, then publish one cumulative Candidate.',
      parameters: {
        preference: { type: 'string', enum: ['cliff', 'notch'], description: 'NX FaceEdgeBlend preference. Defaults to cliff.' },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/fillet-components/delete-all`,
          {
            method: 'POST',
            body: JSON.stringify({
              preference: args.preference || 'cliff',
            }),
          },
          exec.signal,
        )
      },
    })))
    if (config.enableDynamicRuntime === true) {
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_reanalyze_current_state',
        description: 'DEV ONLY: inspect every face in the current persistent NX state and issue fresh state-bound face_ref/component/adjacency references. Curved-face components are only initial fillet candidates, not a deletion whitelist; use all-face face_ref data to reason about missed fillets and related support/closure faces. Required after every successful deletion or rollback.',
        parameters: {},
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(_args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/reanalyze`, { method: 'POST' }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_resolve_accepted_component',
        description: 'DEV ONLY: resolve an Accepted Revision fc-xxx component to its exact runtime state-0 component using STEP face identity. Never infer this mapping from matching ordinals.',
        parameters: { component_id: { type: 'string', required: true, description: 'Accepted Revision component ID such as fc-003.' } },
        output: jsonOutput,
        isConcurrencySafe: () => true,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/resolve-accepted-component`, {
            method: 'POST', body: JSON.stringify({ component_id: args.component_id }),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_get_component_topology',
        description: 'DEV ONLY: get topology metadata for one current candidate component. Components are candidate groups, not necessarily the complete deletion unit; combine this with current all-face references and strategy analysis when support, closure, blend-on-blend, or missed fillet faces are involved.',
        parameters: { component_id: { type: 'string', required: true } },
        output: jsonOutput,
        isConcurrencySafe: () => true,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/component-topology`, {
            method: 'POST', body: JSON.stringify({ component_id: args.component_id }),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_get_face_properties',
        description: 'DEV ONLY: read properties for one face_ref from the current NX runtime state. Returns surface type, stable name/STEP identity, edge count, candidate/component membership, and state-bound adjacent face summaries. Call reanalyze first and never use STEP IDs, NX Tags, guessed ordinals, or stale refs as input.',
        parameters: {
          face_ref: { type: 'string', required: true, description: 'Current state-bound reference such as rs000000-face-15.' },
        },
        output: jsonOutput,
        isConcurrencySafe: () => true,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/face-properties`, {
            method: 'POST', body: JSON.stringify(args),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_segment_fillet_component',
        description: 'DEV ONLY: derive state-bound subsegments of a current component without modifying NX.',
        parameters: {
          component_id: { type: 'string', required: true },
          method: { type: 'string', enum: ['topology_branch', 'simple_chains', 'surface_type', 'bisection'], required: true },
        },
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/segment-component`, {
            method: 'POST', body: JSON.stringify(args),
          }, exec.signal)
        },
      })))
      const deleteParameters = {
        method: { type: 'string', enum: ['face_edge_blend', 'blend', 'generic_face'] },
        preference: { type: 'string', enum: ['cliff', 'notch'] },
      }
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_try_delete_component',
        description: 'DEV ONLY: try one allowlisted deletion on the current candidate component. Commit success is retained and exceptions are undone; if non-target faces disappear, topology protection rejects and rolls back the attempt, returning collateral faces for strategy analysis rather than silently retaining them.',
        parameters: { component_id: { type: 'string', required: true }, ...deleteParameters },
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/try-delete-component`, {
            method: 'POST', body: JSON.stringify(args),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_try_delete_segment',
        description: 'DEV ONLY: try one allowlisted deletion on a state-bound segment returned by segmentation. A segment is not an arbitrary face group and does not include unclassified related faces; use the explicit Face Group tool when strategy analysis confirms a larger deletion unit.',
        parameters: { segment_id: { type: 'string', required: true }, ...deleteParameters },
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/try-delete-segment`, {
            method: 'POST', body: JSON.stringify(args),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_try_delete_face_group',
        description: 'DEV ONLY: try one controlled deletion of an explicit current-state face group. The group may mix recognized fillet faces with unclassified faces confirmed by topology or a prior controlled trial as related support, trace, closure, or blend dependency faces. Do not use STEP IDs, NX Tags, guessed ordinals, or stale refs. Set allow_related_faces only after the expected collateral scope is explicit; Commit success is retained and all failures are undone.',
        parameters: {
          face_refs: { type: 'array', items: { type: 'string' }, required: true },
          method: { type: 'string', enum: ['face_edge_blend', 'blend', 'generic_face'] },
          preference: { type: 'string', enum: ['cliff', 'notch'] },
          allow_related_faces: { type: 'boolean', description: 'Explicitly allow expected related-face disappearance for this dev-only attempt; this does not bypass severe surviving-boundary, Commit, identity, stale-reference, or rollback protection.' },
        },
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/try-delete-face-group`, {
            method: 'POST', body: JSON.stringify(args),
          }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_rollback_latest_delete',
        description: 'DEV ONLY: roll back the latest retained controlled deletion, advance the topology version, and invalidate all current references.',
        parameters: {},
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(_args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-cad/rollback-latest-delete`, { method: 'POST' }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
        name: 'defeature_finish_and_publish',
        description: 'DEV ONLY: export retained NX state once, OCC validate and mesh it, then publish exactly one Candidate.',
        parameters: {},
        output: jsonOutput,
        isConcurrencySafe: () => false,
        async execute(_args, exec) {
          return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-runtime/publish-candidate`, { method: 'POST' }, exec.signal)
        },
      })))
      disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_nx_runtime_status',
      description: 'Get the persistent Siemens NX runtime status, loaded Accepted baseline, process ID, and in-memory runtime state for this DSH session.',
      parameters: {},
      output: jsonOutput,
      isConcurrencySafe: () => true,
      async execute(_args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-runtime/status`, {}, exec.signal)
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_nx_runtime_execute',
      description: 'DEV ONLY: execute dynamic NXOpen Python in the persistent NX session. Use read for inspection, preview for always-rollback trials, and commit to retain an in-memory change. A timeout is ambiguous: query the same operation_id and never retry mutation under a new ID.',
      parameters: {
        operation_id: { type: 'string', required: true, description: 'Stable idempotency key beginning with op-.' },
        base_state: { type: 'integer', required: true, description: 'Current runtime_state returned by status or the preceding operation.' },
        mode: { type: 'string', enum: ['read', 'preview', 'commit'], required: true },
        code: { type: 'string', required: true, description: 'NXOpen Python. Namespace includes NXOpen, session, workPart, displayPart, and ctx.' },
        preview_operation_id: { type: 'string', description: 'Commit-only preview provenance. Omit this field for read and preview calls; it is not required for read-only inspection.' },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/nx-runtime/execute`,
          { method: 'POST', body: JSON.stringify(args) },
          exec.signal,
        )
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_nx_runtime_get_operation',
      description: 'DEV ONLY: reconcile a persistent NX operation by its original operation_id after a timeout or disconnect.',
      parameters: {
        operation_id: { type: 'string', required: true },
      },
      output: jsonOutput,
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-runtime/operations/${encodeURIComponent(args.operation_id)}`, {}, exec.signal)
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_nx_runtime_reset',
      description: 'DEV ONLY: stop the persistent NX worker and reload the current Accepted STEP as a fresh state-0 baseline.',
      parameters: {},
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(_args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-runtime/reset`, { method: 'POST' }, exec.signal)
      },
    })))
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_nx_runtime_publish_candidate',
      description: 'DEV ONLY: export the current persistent NX in-memory model through a fixed trusted command, validate and mesh it with OCC, and publish exactly one Candidate for human review.',
      parameters: {},
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(_args, exec) {
        return callBackend(baseUrl, `/sessions/${sessionId(exec)}/nx-runtime/publish-candidate`, { method: 'POST' }, exec.signal)
      },
    })))
    }
    disposers.push(ctx.tools.register(defineTool({
      name: 'defeature_delete_fillet_components',
      description: 'Delete a restricted batch of validated fillet components from the current accepted revision with an allowlisted NX strategy and publish a Candidate revision on success.',
      parameters: {
        component_ids: {
          type: 'array',
          items: { type: 'string' },
          required: true,
          description: 'Nonempty list of unique component IDs returned for the current accepted revision (maximum 100).',
        },
        strategy: { type: 'string', enum: ['whole-component-face-edge-blend'], description: 'Allowlisted deletion strategy.' },
        preference: { type: 'string', enum: ['cliff', 'notch'], description: 'NX FaceEdgeBlend preference. Defaults to cliff.' },
      },
      output: jsonOutput,
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        const componentIds = args.component_ids
        if (!Array.isArray(componentIds) || componentIds.length === 0 || componentIds.length > 100) {
          throw new Error('component_ids must be a nonempty array with at most 100 entries')
        }
        if (componentIds.some(value => typeof value !== 'string' || value.length === 0)) {
          throw new Error('component_ids must contain only nonempty component ID strings')
        }
        if (new Set(componentIds).size !== componentIds.length) {
          throw new Error('component_ids must not contain duplicates')
        }
        return callBackend(
          baseUrl,
          `/sessions/${sessionId(exec)}/fillet-components/delete-batch`,
          {
            method: 'POST',
            body: JSON.stringify({
              component_ids: args.component_ids,
              strategy: args.strategy || 'whole-component-face-edge-blend',
              preference: args.preference || 'cliff',
            }),
          },
          exec.signal,
        )
      },
    })))
    return () => { for (const dispose of disposers.reverse()) dispose() }
  }, 'defeature: scoped CAD tools')
}
