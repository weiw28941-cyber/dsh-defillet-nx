import assert from 'node:assert/strict'
import { apply } from '../lib/tools.js'

const registered = []
let cleanup = () => {}
const tools = { register(tool) { registered.push(tool); return () => {} } }
const attachments = {
  async saveImage({ data, mediaType, name }) {
    return { attachmentId: 'sha256:test', mediaType, bytes: data.length, width: 1220, height: 760, name }
  },
}
const llm = { async resolveModelInfo() { return { inputModalities: ['text', 'image'] } } }
const ctx = { tools, attachments, llm, effect(run) { cleanup = run() } }
apply(ctx, { baseUrl: 'http://127.0.0.1:3080' })

assert.deepEqual(registered.map(tool => tool.name).sort(), [
  'defeature_analyze_fillet_components',
  'defeature_delete_fillet_component',
  'defeature_delete_fillet_components',
  'defeature_detect_fillet_components',
  'defeature_get_context',
  'defeature_get_selection',
  'defeature_remove_all_fillets',
  'defeature_rollback_accepted_revision',
])
assert.deepEqual(registered.find(tool => tool.name === 'defeature_get_context').parameters.properties, {})
assert.deepEqual(registered.find(tool => tool.name === 'defeature_get_selection').parameters.properties, {})
const rollbackAccepted = registered.find(tool => tool.name === 'defeature_rollback_accepted_revision')
assert.deepEqual(rollbackAccepted.parameters.required, ['target_revision'])
assert.equal(rollbackAccepted.parameters.properties.target_revision.type, 'string')
const analyzeAll = registered.find(tool => tool.name === 'defeature_analyze_fillet_components')
assert.deepEqual(analyzeAll.parameters.properties, {})

const batchDelete = registered.find(tool => tool.name === 'defeature_delete_fillet_components')
assert.deepEqual(batchDelete.parameters.required, ['component_ids'])
assert.deepEqual(batchDelete.parameters.properties.component_ids, {
  type: 'array',
  items: { type: 'string' },
  description: 'Nonempty list of unique component IDs returned for the current accepted revision (maximum 100).',
})
assert.deepEqual(batchDelete.parameters.properties.strategy.enum, ['whole-component-face-edge-blend'])
assert.deepEqual(batchDelete.parameters.properties.preference.enum, ['cliff', 'notch'])
assert.equal(batchDelete.parameters.properties.delete_partial_blend, undefined)

const removeAll = registered.find(tool => tool.name === 'defeature_remove_all_fillets')
assert.equal(removeAll.parameters.required, undefined)
assert.deepEqual(removeAll.parameters.properties.preference.enum, ['cliff', 'notch'])

const getSelection = registered.find(tool => tool.name === 'defeature_get_selection')
const originalFetch = globalThis.fetch
let analyzeRequests = []
try {
  globalThis.fetch = async (url, init) => {
    analyzeRequests.push({ url, init })
    if (url.endsWith('/analysis-image')) {
      return new Response(new Uint8Array([137, 80, 78, 71]), {
        status: 200,
        headers: { 'content-type': 'image/png' },
      })
    }
    return new Response(JSON.stringify({
      status: 'ready_for_visual_analysis',
      image_name: 'fillet-components.png',
      job_id: 'job-1',
      revision_id: 'r0',
      legend: [{ component_id: 'fc-001', color: '#ef4444' }],
    }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  const result = await analyzeAll.execute({}, {
    agent: { id: 'session/one', options: { provider: 'test', model: 'vision' } },
    signal: undefined,
  })
  assert.equal(result.image.attachmentId, 'sha256:test')
  assert.equal(analyzeRequests.length, 2)
  assert.equal(analyzeRequests[0].url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/fillet-components/analyze')
  assert.equal(analyzeRequests[1].url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/fillet-components/analysis-image/job-1/r0')
  assert.equal(analyzeAll.output.render({}, result)[1].type, 'image')
} finally {
  globalThis.fetch = originalFetch
}

let selectionRequest
try {
  globalThis.fetch = async (url, init) => {
    selectionRequest = { url, init }
    return new Response(JSON.stringify({ selected_face_count: 1 }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await getSelection.execute({}, { agent: { id: 'session/one' }, signal: undefined })
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(selectionRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/selection')

let rollbackRequest
try {
  globalThis.fetch = async (url, init) => {
    rollbackRequest = { url, init }
    return new Response(JSON.stringify({ accepted_revision: 'r0' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await rollbackAccepted.execute(
    { target_revision: 'r0' },
    { agent: { id: 'session/one' }, signal: undefined },
  )
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(rollbackRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/revisions/rollback')
assert.deepEqual(JSON.parse(rollbackRequest.init.body), { target_revision: 'r0' })

let globalRequest
try {
  globalThis.fetch = async (url, init) => {
    globalRequest = { url, init }
    return new Response(JSON.stringify({ status: 'succeeded' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await removeAll.execute(
    { preference: 'notch' },
    { agent: { id: 'session/one' }, signal: undefined },
  )
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(globalRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/fillet-components/delete-all')
assert.deepEqual(JSON.parse(globalRequest.init.body), {
  preference: 'notch',
})

let batchRequest
try {
  globalThis.fetch = async (url, init) => {
    batchRequest = { url, init }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await batchDelete.execute(
    {
      component_ids: ['fc-001', 'fc-002'],
      strategy: 'whole-component-face-edge-blend',
      preference: 'notch',
    },
    { agent: { id: 'session/one' }, signal: undefined },
  )
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(batchRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/fillet-components/delete-batch')
assert.equal(batchRequest.init.method, 'POST')
assert.deepEqual(JSON.parse(batchRequest.init.body), {
  component_ids: ['fc-001', 'fc-002'],
  strategy: 'whole-component-face-edge-blend',
  preference: 'notch',
})

for (const failureCategory of ['geometry', 'infrastructure']) {
  const failureDetail = { status: 'failed', failure_category: failureCategory, rollback: true }
  let batchError
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: failureDetail }), {
      status: 422,
      headers: { 'content-type': 'application/json' },
    })
    await batchDelete.execute(
      { component_ids: ['fc-001'] },
      { agent: { id: 'session-one' }, signal: undefined },
    )
  } catch (error) {
    batchError = error
  } finally {
    globalThis.fetch = originalFetch
  }
  assert(batchError instanceof Error)
  assert.deepEqual(JSON.parse(batchError.message), failureDetail)
  assert.equal(JSON.parse(batchError.message).failure_category, failureCategory)
}
assert.doesNotThrow(() => cleanup())

const devRegistered = []
let devCleanup = () => {}
apply({
  tools: { register(tool) { devRegistered.push(tool); return () => {} } },
  attachments,
  llm,
  effect(run) { devCleanup = run() },
}, { baseUrl: 'http://127.0.0.1:3080', enableDynamicRuntime: true })
assert.deepEqual(
  devRegistered.map(tool => tool.name).filter(name =>
    name.startsWith('defeature_nx_runtime_') || [
      'defeature_reanalyze_current_state',
      'defeature_resolve_accepted_component',
      'defeature_get_component_topology',
      'defeature_get_face_properties',
      'defeature_rollback_latest_delete',
      'defeature_segment_fillet_component',
      'defeature_try_delete_component',
      'defeature_try_delete_segment',
      'defeature_try_delete_face_group',
      'defeature_finish_and_publish',
    ].includes(name)
  ).sort(),
  [
    'defeature_finish_and_publish',
    'defeature_get_component_topology',
    'defeature_get_face_properties',
    'defeature_nx_runtime_execute',
    'defeature_nx_runtime_get_operation',
    'defeature_nx_runtime_publish_candidate',
    'defeature_nx_runtime_reset',
    'defeature_nx_runtime_status',
    'defeature_reanalyze_current_state',
    'defeature_resolve_accepted_component',
    'defeature_rollback_latest_delete',
    'defeature_segment_fillet_component',
    'defeature_try_delete_component',
    'defeature_try_delete_face_group',
    'defeature_try_delete_segment',
  ],
)
const productionNames = new Set(registered.map(tool => tool.name))
for (const name of [
  'defeature_reanalyze_current_state',
  'defeature_resolve_accepted_component',
  'defeature_get_component_topology',
  'defeature_rollback_latest_delete',
  'defeature_segment_fillet_component',
  'defeature_try_delete_component',
  'defeature_try_delete_segment',
  'defeature_finish_and_publish',
]) assert.equal(productionNames.has(name), false)

const tryDelete = devRegistered.find(tool => tool.name === 'defeature_try_delete_component')
const faceProperties = devRegistered.find(tool => tool.name === 'defeature_get_face_properties')
assert.deepEqual(faceProperties.parameters.required, ['face_ref'])
let facePropertiesRequest
try {
  globalThis.fetch = async (url, init) => {
    facePropertiesRequest = { url, init }
    return new Response(JSON.stringify({ schema: 'nx_face_properties_v1' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await faceProperties.execute({ face_ref: 'rs000000-face-15' }, {
    agent: { id: 'session/one' }, signal: undefined,
  })
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(facePropertiesRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/nx-cad/face-properties')
assert.deepEqual(JSON.parse(facePropertiesRequest.init.body), { face_ref: 'rs000000-face-15' })
const runtimeExecute = devRegistered.find(tool => tool.name === 'defeature_nx_runtime_execute')
assert.equal(runtimeExecute.parameters.properties.preview_operation_id.required, undefined)
assert.deepEqual(tryDelete.parameters.properties.method.enum, ['face_edge_blend', 'blend', 'generic_face'])
assert.deepEqual(tryDelete.parameters.properties.preference.enum, ['cliff', 'notch'])
assert.equal(tryDelete.parameters.properties.delete_partial_blend, undefined)
let controlledRequest
try {
  globalThis.fetch = async (url, init) => {
    controlledRequest = { url, init }
    return new Response(JSON.stringify({ commit_succeeded: false, rolled_back: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await tryDelete.execute({
    component_id: 'rs000000-fc-0001', method: 'blend', preference: 'notch',
  }, { agent: { id: 'session/one' }, signal: undefined })
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(controlledRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/nx-cad/try-delete-component')
assert.deepEqual(JSON.parse(controlledRequest.init.body), {
  component_id: 'rs000000-fc-0001', method: 'blend', preference: 'notch',
})

const tryDeleteFaceGroup = devRegistered.find(tool => tool.name === 'defeature_try_delete_face_group')
assert.deepEqual(tryDeleteFaceGroup.parameters.properties.method.enum, ['face_edge_blend', 'blend', 'generic_face'])
assert.deepEqual(tryDeleteFaceGroup.parameters.properties.preference.enum, ['cliff', 'notch'])
assert.equal(tryDeleteFaceGroup.parameters.properties.allow_related_faces.type, 'boolean')
let faceGroupRequest
try {
  globalThis.fetch = async (url, init) => {
    faceGroupRequest = { url, init }
    return new Response(JSON.stringify({ commit_succeeded: true }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
  }
  await tryDeleteFaceGroup.execute({
    face_refs: ['rs000000-face-1', 'rs000000-face-2'],
    method: 'generic_face', preference: 'cliff', allow_related_faces: true,
  }, { agent: { id: 'session/one' }, signal: undefined })
} finally {
  globalThis.fetch = originalFetch
}
assert.equal(faceGroupRequest.url, 'http://127.0.0.1:3080/defeature/api/sessions/session%2Fone/nx-cad/try-delete-face-group')
assert.deepEqual(JSON.parse(faceGroupRequest.init.body), {
  face_refs: ['rs000000-face-1', 'rs000000-face-2'],
  method: 'generic_face', preference: 'cliff', allow_related_faces: true,
})

assert.doesNotThrow(() => devCleanup())
console.log('Defeature CAD tools registered with production/development separation')
