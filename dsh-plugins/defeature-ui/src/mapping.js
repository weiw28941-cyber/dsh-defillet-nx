export function parseMappingJson(text) {
  return JSON.parse(String(text).replace(/^\uFEFF/, ''))
}

function parseStepRef(value) {
  if (Number.isInteger(value) && value > 0) return value
  if (typeof value !== 'string') return null
  const match = value.trim().match(/^#?(\d+)$/)
  if (!match) return null
  const id = Number(match[1])
  return id > 0 ? id : null
}

/** Extract accepted STEP ADVANCED_FACE entity IDs from supported mapping reports. */
export function extractMappedStepFaceIds(payload) {
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.matches)) {
    throw new Error('JSON 缺少 matches 数组')
  }
  const ids = new Set()
  for (const match of payload.matches) {
    if (!match || typeof match !== 'object') continue
    const mapping = match.mapping && typeof match.mapping === 'object' ? match.mapping : match
    if (mapping.accepted !== true) continue
    const id = parseStepRef(mapping.step_face_ref ?? mapping.best_step_ref)
    if (id !== null) ids.add(id)
  }
  if (!ids.size) throw new Error('未找到 accepted=true 的 STEP 面编号')
  return [...ids].sort((a, b) => a - b)
}
