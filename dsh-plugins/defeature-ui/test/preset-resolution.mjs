import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

// Use the host's YAML dialect and module search roots, not a generic YAML parser.
const host = process.env.DEFEATURE_TEST_DSH_ROOT
if (!host) throw new Error('Set DEFEATURE_TEST_DSH_ROOT to the installed DSH package directory')
const require = createRequire(join(host, 'package.json'))
const includePath = require.resolve('@deepseek-ai/cordis-plugin-include')
const { entryListSchema } = await import(pathToFileURL(includePath))
const yaml = createRequire(includePath)('js-yaml')
const plugin = resolve(import.meta.dirname, '..', 'dist', 'package')
const metadata = JSON.parse(await readFile(join(plugin, 'package.json'), 'utf8'))
let count = 0
async function validate(rows) {
  assert.ok(Array.isArray(rows))
  const ids = new Set()
  for (const row of rows) {
    assert.ok(!ids.has(row.id), 'Duplicate preset ID: ' + row.id)
    ids.add(row.id)
    if (row.name === 'cordis:group') {
      assert.equal(row.group, true)
      await validate(row.config)
      continue
    }
    let modulePath
    if (row.name.startsWith(metadata.name + '/')) {
      const key = '.' + row.name.slice(metadata.name.length)
      assert.ok(metadata.exports[key], 'Missing package export: ' + key)
      modulePath = join(plugin, metadata.exports[key])
    } else modulePath = require.resolve(row.name)
    const module = await import(pathToFileURL(modulePath))
    assert.ok(typeof module.apply === 'function' || typeof module.default === 'function' ||
      typeof module.default?.apply === 'function', 'Invalid Cordis plugin: ' + row.name)
    count++
  }
}
for (const preset of ['defeature-agent', 'defeature-agent-dev']) {
  const text = await readFile(join(plugin, 'presets', preset, 'agent.cordis.yml'), 'utf8')
  await validate(yaml.load(text, { schema: entryListSchema }))
}
console.log('Host YAML dialect and preset plugin exports verified:', count, 'entries')
