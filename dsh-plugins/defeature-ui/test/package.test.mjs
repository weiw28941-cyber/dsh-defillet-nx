import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { dataRoot, exportDevelopmentSource, installPresets, readInstallation, saveInstallation } from '../lib/installation.js'
import { apply } from '../lib/index.js'

const plugin = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const stage = join(plugin, 'dist', 'package')

test('release runtime matches only the explicit allowlist and recorded hashes', async () => {
  const release = JSON.parse(await readFile(join(stage, 'release-manifest.json'), 'utf8'))
  const allowlist = JSON.parse(await readFile(join(plugin, 'release-files.json'), 'utf8')).runtime
  assert.deepEqual(release.runtimeAllowlist, allowlist)
  for (const directory of ['Defillet', 'OccUtils']) {
    assert.ok(allowlist.some(path => path.startsWith(`cad/${directory}/`)))
    assert.ok(!allowlist.some(path => path.startsWith(`${directory}/`)))
  }
  assert.ok(allowlist.includes('cad/nx_persistent_worker.py'))
  const runtime = release.files.filter(row => row.path.startsWith('runtime/')).map(row => row.path.slice(8))
  assert.deepEqual(runtime.sort(), [...allowlist].sort())
  for (const row of release.files) {
    assert.doesNotMatch(row.path, /(?:node_modules|__pycache__|\.git\/|\.env$|\.pdf$|\.prt$|\.stp$|\.step$|archive\/|Legacy\/)/)
    const bytes = await readFile(join(stage, row.path))
    assert.equal(bytes.length, row.bytes, row.path)
    assert.equal(createHash('sha256').update(bytes).digest('hex'), row.sha256, row.path)
  }
  assert.ok(release.files.some(row => row.path === 'client/client.js'))
  assert.ok(release.files.some(row => row.path === 'skills/nx-delete-fillet-component/SKILL.md'))
  assert.ok(release.files.some(row => row.path === 'skills/fillet-removal-strategy/references/strategy-background.md'))
})

test('presets have no author-machine paths and production excludes development grants', async () => {
  const production = await readFile(join(stage, 'presets/defeature-agent/agent.cordis.yml'), 'utf8')
  const development = await readFile(join(stage, 'presets/defeature-agent-dev/agent.cordis.yml'), 'utf8')
  for (const content of [production, development]) assert.doesNotMatch(content, /weiwh40|Desktop|name: ['"]?[A-Z]:\\/)
  assert.doesNotMatch(production, /name: .*dsh-tool-(?:bash|pwsh|fs|subagent)|enableDynamicRuntime: true/)
  assert.match(development, /enableDynamicRuntime: true/)
  assert.match(development, /dsh-tool-pwsh/)
  assert.match(development, /dsh-tool-fs/)
  assert.match(development, /dsh-tool-subagent/)
})

test('preset installation never overwrites user customizations', async () => {
  const home = await mkdtemp(join(tmpdir(), 'defeature-presets-'))
  try {
    installPresets(home)
    const file = join(home, '.agent-presets/defeature-agent/agent.cordis.yml')
    await writeFile(file, 'user customization')
    installPresets(home)
    assert.equal(await readFile(file, 'utf8'), 'user customization')
    assert.ok(existsSync(join(home, '.agent-presets/defeature-agent-dev/agent.cordis.yml')))
  } finally { await rm(home, { recursive: true, force: true }) }
})

test('NX and Python settings persist outside the package', async () => {
  const root = await mkdtemp(join(tmpdir(), 'defeature-settings-'))
  try {
    const config = { dataRoot: root }
    saveInstallation(config, { nxBase: 'C:\\NX' })
    saveInstallation(config, { pythonExecutable: 'C:\\Python\\python.exe' })
    assert.deepEqual(readInstallation(config), { nxBase: 'C:\\NX', pythonExecutable: 'C:\\Python\\python.exe' })
    assert.equal(dataRoot(config), resolve(root))
  } finally { await rm(root, { recursive: true, force: true }) }
})

test('setup cannot install without same-origin explicit confirmation', async () => {
  const root = await mkdtemp(join(tmpdir(), 'defeature-setup-'))
  const oldHome = process.env.DSH_HOME
  process.env.DSH_HOME = root
  let cleanup
  const routes = []
  try {
    apply({ inject(_services, callback) {
      callback({ webServer: { register(route) { routes.push(route); return () => {} } }, effect(run) { cleanup = run() } })
    } }, { dataRoot: root, pythonExecutable: join(root, 'missing-python.exe') })
    await new Promise(done => setTimeout(done, 20))
    const handler = routes.find(route => route.path === '/defeature/setup').handler
    async function request(headers, body) {
      const incoming = { method: 'POST', headers, async *[Symbol.asyncIterator]() { yield JSON.stringify(body) } }
      let payload
      const outgoing = { statusCode: 0, setHeader() {}, end(text) { payload = JSON.parse(text) } }
      await handler(incoming, outgoing)
      return { status: outgoing.statusCode, payload }
    }
    assert.equal((await request({ host: 'localhost:3080', origin: 'https://evil.invalid', 'content-type': 'application/json' },
      { action: 'install-environment', confirmed: true })).status, 403)
    assert.equal((await request({ host: 'localhost:3080', origin: 'http://localhost:3080', 'content-type': 'application/json' },
      { action: 'install-environment', confirmed: false })).status, 400)
    let snapshot
    routes.find(route => route.path === '/defeature/status').handler({}, {
      setHeader() {}, end(text) { snapshot = JSON.parse(text) },
    })
    assert.equal(snapshot.state, 'setup_required')
    assert.equal(snapshot.python, join(root, 'missing-python.exe'))
    assert.ok(!existsSync(join(root, 'installer')))
  } finally {
    if (cleanup) await cleanup()
    if (oldHome === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = oldHome
    await rm(root, { recursive: true, force: true })
  }
})
