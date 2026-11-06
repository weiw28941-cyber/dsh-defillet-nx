import { cp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const plugin = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repo = resolve(plugin, '../..')
const manifest = JSON.parse(await readFile(join(plugin, 'release-files.json'), 'utf8'))
const output = join(plugin, 'dist')
const stage = join(output, 'package')
await mkdir(output, { recursive: true })
await rm(stage, { recursive: true, force: true })
await mkdir(stage)
const metadata = JSON.parse(await readFile(join(plugin, 'package.json'), 'utf8'))
for (const item of ['lib', 'src', 'client', 'presets', 'environment.yml', 'cordis.patch.yml', 'README.md']) {
  await cp(join(plugin, item), join(stage, item), { recursive: true })
}
for (const path of manifest.runtime) {
  const destination = join(stage, 'runtime', path)
  await mkdir(dirname(destination), { recursive: true })
  await cp(join(repo, path), destination)
}
for (const skill of manifest.skills) {
  await mkdir(dirname(join(stage, 'skills', skill)), { recursive: true })
  await cp(join(repo, '.dsh', 'skills', skill), join(stage, 'skills', skill))
}
// Convert repo-specific reference links to paths inside the shipped skill tree.
async function rewriteReferences(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) await rewriteReferences(path)
    else if (entry.name.endsWith('.md')) {
      const text = await readFile(path, 'utf8')
      const skillsRoot = relative(dirname(path), join(stage, 'skills')).replaceAll('\\', '/')
      await writeFile(path, text.replaceAll('.dsh/skills/', skillsRoot + '/'))
    }
  }
}
await rewriteReferences(join(stage, 'skills'))
delete metadata.devDependencies
metadata.scripts = { 'export-source': 'node lib/export-source.js' }
await writeFile(join(stage, 'package.json'), JSON.stringify(metadata, null, 2) + '\n')
const files = []
async function inventory(directory, prefix = '') {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const relative = prefix + entry.name
    if (entry.isDirectory()) await inventory(join(directory, entry.name), relative + '/')
    else {
      const bytes = await readFile(join(directory, entry.name))
      files.push({ path: relative, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') })
    }
  }
}
await inventory(stage)
await writeFile(join(stage, 'release-manifest.json'), JSON.stringify({ version: metadata.version,
  runtimeAllowlist: manifest.runtime, files: files.sort((a, b) => a.path.localeCompare(b.path)),
}, null, 2) + '\n')
if (!process.env.npm_execpath) throw new Error('Run this release script through npm run release')
const result = spawnSync(process.execPath, [process.env.npm_execpath, 'pack', '--ignore-scripts', '--pack-destination', output], {
  cwd: stage, stdio: 'inherit',
})
if (result.status !== 0) process.exit(result.status || 1)
console.log('Release staged at ' + stage)
