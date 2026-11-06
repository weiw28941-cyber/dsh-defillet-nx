import assert from 'node:assert/strict'
import { extractMappedStepFaceIds, parseMappingJson } from '../src/mapping.js'

const compact = parseMappingJson('\uFEFF' + JSON.stringify({
  schema: 'ansys-step-face-match-compact/v1',
  source_step: { path: 'ignored.stp' },
  matches: [
    { accepted: true, step_face_ref: '#1865' },
    { accepted: true, step_face_ref: '#42' },
    { accepted: false, step_face_ref: '#9' },
    { accepted: true, step_face_ref: '#1865' },
  ],
}))
assert.deepEqual(extractMappedStepFaceIds(compact), [42, 1865])
assert.deepEqual(extractMappedStepFaceIds({ matches: [
  { mapping: { accepted: true, best_step_ref: '#42' } },
  { mapping: { accepted: true, best_step_ref: '#42' } },
  { accepted: true, step_face_ref: '#7' },
  { accepted: false, step_face_ref: '#9' },
]}), [7, 42])
assert.throws(() => extractMappedStepFaceIds({ matches: [] }), /未找到/)
console.log('mapping parser accepts compact, legacy, duplicate, and BOM inputs')
