import assert from 'node:assert/strict'
import { apply } from '../lib/index.js'

const routes = []
let cleanup = async () => {}
const webServer = { register(route) { routes.push(route); return () => {} } }
apply({ inject(_services, callback) { callback({ webServer, effect(run) { cleanup = run() } }) } }, { startupTimeoutMs: 30_000 })

const statusRoute = routes.find(route => route.path === '/defeature/status')
const proxyRoute = routes.find(route => route.path === '/defeature/api')
assert.equal(statusRoute?.kind, 'exact')
assert.equal(proxyRoute?.kind, 'prefix')

function status() {
  let payload = ''
  const response = { statusCode: 0, setHeader() {}, end(value = '') { payload = value } }
  statusRoute.handler({}, response)
  return JSON.parse(payload)
}

try {
  const deadline = Date.now() + 35_000
  while (Date.now() < deadline && status().state === 'starting') await new Promise(resolve => setTimeout(resolve, 250))
  const ready = status()
  assert.equal(ready.state, 'ready', JSON.stringify(ready))
  const health = await fetch(`http://127.0.0.1:${ready.port}/api/health`)
  assert.equal(health.status, 200)
  assert.equal((await health.json()).ok, true)
  console.log(`managed backend ready on ${ready.port}`)
} finally {
  await cleanup()
}
