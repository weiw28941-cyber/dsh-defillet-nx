import type { Job, JobEvent, MeshPayload, PickedFace } from '../types'

async function responseError(response: Response) {
  const text = await response.text()
  try {
    const payload = JSON.parse(text) as { detail?: unknown }
    if (typeof payload.detail === 'string') return new Error(payload.detail)
  } catch {
    // Preserve non-JSON provider or server responses verbatim.
  }
  return new Error(text || `HTTP ${response.status}`)
}

export async function uploadStep(file: File) {
  const data = new FormData(); data.append('file', file)
  const response = await fetch('/api/jobs', { method: 'POST', body: data })
  if (!response.ok) throw await responseError(response)
  return response.json() as Promise<{jobId:string; revisionId:string; status:string}>
}
export async function getJob(jobId: string) {
  const response = await fetch(`/api/jobs/${jobId}`)
  if (!response.ok) throw await responseError(response)
  return response.json() as Promise<Job>
}
export async function getMesh(jobId: string, revisionId: string) {
  const response = await fetch(`/api/jobs/${jobId}/revisions/${revisionId}/mesh`)
  if (!response.ok) throw await responseError(response)
  return response.json() as Promise<MeshPayload>
}
export async function getEvents(jobId:string, after=0) {
  const response = await fetch(`/api/jobs/${jobId}/event-log?after=${after}`)
  if (!response.ok) throw await responseError(response)
  return response.json() as Promise<JobEvent[]>
}
export async function execute(jobId:string, revisionId:string, prompt:string, selected:PickedFace[]) {
  const response = await fetch(`/api/jobs/${jobId}/execute`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision_id:revisionId,prompt,selected_faces:selected.map(face=>({face_id:face.faceId,face_index:face.faceIndex,step_entity_id:face.stepEntityId,pick_point:face.pickPoint}))})})
  if (!response.ok) throw await responseError(response)
  return response.json() as Promise<Job>
}
