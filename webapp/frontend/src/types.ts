export type FaceData = {
  faceId: string
  faceIndex: number
  freecadFace: string
  stepEntityId: number | null
  stepEntity: string | null
  entityType: string
  surfaceType: string
  area: number
  centroid: [number, number, number]
  positions: number[]
  indices: number[]
}

export type MeshPayload = { revisionId: string; faceCount: number; faces: FaceData[] }
export type PickedFace = FaceData & { pickPoint: [number, number, number] }
export type Job = { id: string; status: string; current_revision: string; error?: string | null }
export type JobEvent = { seq: number; job_id: string; created_at: string; kind: string; message: string }
