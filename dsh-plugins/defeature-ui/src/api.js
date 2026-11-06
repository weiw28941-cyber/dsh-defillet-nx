export const API = '/defeature/api'
export const STATUS_API = '/defeature/status'

async function api(path, init) {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) {
    let message = await response.text()
    try {
      message = JSON.parse(message).detail || message
    } catch {}
    throw new Error(message || `HTTP ${response.status}`)
  }
  return (response.headers.get('content-type') || '').includes('json')
    ? response.json()
    : response
}

export async function uploadJob(sessionId, file) {
  const body = new FormData()
  body.append('file', file)
  return api(`/sessions/${encodeURIComponent(sessionId)}/jobs`, {
    method: 'POST',
    body,
  })
}

export const recoverJob = sessionId =>
  api(`/sessions/${encodeURIComponent(sessionId)}/job`)

export const getJob = id => api(`/jobs/${id}`)

export const setSelection = (sessionId, revisionId, selectedFaces, signal) =>
  api(`/sessions/${encodeURIComponent(sessionId)}/selection`, {
    method: 'PUT',
    signal,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      revision_id: revisionId,
      selected_faces: selectedFaces.map(face => ({
        face_id: face.faceId,
        face_index: face.faceIndex,
        step_entity_id: face.stepEntityId ?? null,
        pick_point: face.pickPoint || face.centroid || [0, 0, 0],
      })),
    }),
  })

export const getMesh = (id, revision) =>
  api(`/jobs/${id}/revisions/${revision}/mesh`)

export const getRevisions = id => api(`/jobs/${id}/revisions`)

export const getEvents = (id, after = 0) =>
  api(`/jobs/${id}/event-log?after=${after}`)

export const acceptCandidate = id =>
  api(`/jobs/${id}/candidate/accept`, { method: 'POST' })

export const rejectCandidate = id =>
  api(`/jobs/${id}/candidate/reject`, { method: 'POST' })

export const detectFillets = (id, revision, force = false) =>
  api(`/jobs/${id}/revisions/${revision}/detect-fillets?force=${force}`, {
    method: 'POST',
  })

export const getNxRuntimeStatus = sessionId =>
  api(`/sessions/${encodeURIComponent(sessionId)}/nx-runtime/status`)
