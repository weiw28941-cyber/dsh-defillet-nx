import { build } from 'esbuild'
import { readFile, writeFile, rm } from 'node:fs/promises'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const temp = resolve(root, 'client/.client-body.cjs')
const output = resolve(root, 'client/client.js')

await build({
  entryPoints: [resolve(root, 'src/client.js')],
  outfile: temp,
  bundle: true,
  platform: 'browser',
  format: 'cjs',
  target: ['es2022'],
  external: ['react'],
  sourcemap: false,
  minify: false,
  logLevel: 'info',
})
const body = await readFile(temp, 'utf8')
const wrapped = `window.__ModuleLoader__.load({ id: "@defeature/dsh-ui", factory: (require) => {\nvar module = { exports: {} };\nvar exports = module.exports;\n${body}\nreturn module.exports;\n}});\n`
await writeFile(output, wrapped, 'utf8')
await rm(temp)
console.log(`built ${output}`)
