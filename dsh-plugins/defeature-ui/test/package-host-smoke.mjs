import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const root = await mkdtemp(join(tmpdir(), 'defeature-package-host-'))
process.env.DSH_HOME = root
const { apply } = await import('../dist/package/lib/index.js')
const routes = []
let cleanup
apply({ inject(_services, callback) {
  callback({ webServer: { register(route) { routes.push(route); return () => {} } }, effect(run) { cleanup = run() } })
} }, { dataRoot: root, pythonExecutable: process.env.DEFEATURE_TEST_PYTHON })
const statusRoute = routes.find(route => route.path === '/defeature/status')
function status() {
  let result
  statusRoute.handler({}, { setHeader() {}, end(value) { result = JSON.parse(value) } })
  return result
}
try {
  const deadline = Date.now() + 45000
  while (Date.now() < deadline && ['starting', 'stopped'].includes(status().state)) {
    await new Promise(done => setTimeout(done, 250))
  }
  const snapshot = status()
  assert.equal(snapshot.state, 'ready', JSON.stringify(snapshot))
  assert.match(snapshot.repoRoot.replaceAll('\\', '/'), /dist\/package\/runtime$/)
  const response = await fetch(`http://127.0.0.1:${snapshot.port}/api/health`)
  assert.equal(response.status, 200)
  assert.equal((await response.json()).ok, true)
  const setup = routes.find(route => route.path === '/defeature/setup')
  let saved
  const configRequest = { method: 'POST', headers: {
    host: 'localhost:3080', origin: 'http://localhost:3080', 'content-type': 'application/json',
  }, async *[Symbol.asyncIterator]() { yield JSON.stringify({ action: 'configure', nxBase: 'C:\\NX-test' }) } }
  await setup.handler(configRequest, { setHeader() {}, end(text) { saved = JSON.parse(text) } })
  assert.equal(saved.restartRequired, true)
  assert.equal(status().port, snapshot.port)
  assert.equal(status().state, 'ready')
  assert.equal(JSON.parse(await readFile(join(root, 'config.json'), 'utf8')).nxBase, 'C:\\NX-test')
  console.log(`Packaged backend verified at http://127.0.0.1:${snapshot.port}/api/health`)
} finally {
  await cleanup()
  await new Promise(done => setTimeout(done, 500))
  await rm(root, { recursive: true, force: true })
}

// Disposal during port reservation must not spawn a backend or leave startup polling.
let disposeStarting
apply({ inject(_services, callback) {
  callback({ webServer: { register() { return () => {} } }, effect(run) { disposeStarting = run(); return disposeStarting() } })
} }, { dataRoot: root, pythonExecutable: process.env.DEFEATURE_TEST_PYTHON })
await new Promise(done => setTimeout(done, 500))
await rm(root, { recursive: true, force: true })
console.log('Running configuration and startup disposal verified')
