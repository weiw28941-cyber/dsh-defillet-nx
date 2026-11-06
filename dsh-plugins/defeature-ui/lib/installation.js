import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { cp, rename, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'

export const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
export function dataRoot(config = {}) {
  return resolve(config.dataRoot || process.env.DEFEATURE_DATA_ROOT ||
    join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'defeature'))
}

export function installPresets(root = process.env.DSH_HOME || join(homedir(), '.dsh')) {
  for (const id of ['defeature-agent', 'defeature-agent-dev']) {
    const source = join(packageRoot, 'presets', id)
    if (!existsSync(source)) continue
    const target = join(root, '.agent-presets', id)
    mkdirSync(target, { recursive: true })
    for (const file of ['agent.cordis.yml', 'preset.yml']) {
      const destination = join(target, file)
      if (!existsSync(destination)) writeFileSync(destination, readFileSync(join(source, file)), { flag: 'wx' })
    }
  }
}

export function readInstallation(config = {}) {
  const path = join(dataRoot(config), 'config.json')
  if (!existsSync(path)) return {}
  const value = JSON.parse(readFileSync(path, 'utf8'))
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Defeature config.json')
  return value
}

export function saveInstallation(config, values) {
  const root = dataRoot(config)
  mkdirSync(root, { recursive: true })
  const path = join(root, 'config.json')
  writeFileSync(path, JSON.stringify({ ...readInstallation(config), ...values }, null, 2) + '\n')
}

function run(command, args, record) {
  return new Promise((done, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    child.stdout.on('data', record)
    child.stderr.on('data', record)
    child.once('error', reject)
    child.once('exit', code => code === 0 ? done() : reject(new Error(`${command} exited with code ${code}`)))
  })
}

export async function installEnvironment(config, record = () => {}) {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error('The automatic NX environment installer currently supports Windows x64 only')
  }
  const root = dataRoot(config)
  const prefix = join(root, 'python-env')
  const installerDir = join(root, 'installer')
  const miniforge = join(root, 'miniforge')
  mkdirSync(installerDir, { recursive: true })
  const conda = join(miniforge, 'Scripts', 'conda.exe')
  if (!existsSync(conda)) {
    const filename = 'Miniforge3-Windows-x86_64.exe'
    const base = 'https://github.com/conda-forge/miniforge/releases/latest/download/'
    record('Downloading Miniforge from conda-forge; verifying published SHA-256\n')
    const release = await fetch(base + filename, { redirect: 'manual', signal: AbortSignal.timeout(60_000) })
    const location = release.headers.get('location')
    if (release.status !== 302 || !location) throw new Error('Cannot resolve Miniforge release')
    const download = new URL(location, base)
    if (download.origin !== 'https://github.com' || !download.pathname.startsWith('/conda-forge/miniforge/releases/download/')) {
      throw new Error('Unexpected Miniforge release URL')
    }
    const [binary, checksum] = await Promise.all([
      fetch(download, { signal: AbortSignal.timeout(300_000) }),
      fetch(download.href + '.sha256', { signal: AbortSignal.timeout(60_000) }),
    ])
    if (!binary.ok || !checksum.ok) throw new Error('Miniforge download failed')
    const bytes = Buffer.from(await binary.arrayBuffer())
    const expected = (await checksum.text()).trim().split(/\s+/)[0]
    if (!/^[a-f0-9]{64}$/i.test(expected) || createHash('sha256').update(bytes).digest('hex') !== expected.toLowerCase()) {
      throw new Error('Miniforge SHA-256 verification failed')
    }
    const installer = join(installerDir, filename)
    await writeFile(installer, bytes)
    await run(installer, ['/S', '/InstallationType=JustMe', '/RegisterPython=0', '/AddToPath=0', `/D=${miniforge}`], record)
  }
  const specs = ['python=3.11', 'pythonocc-core=7.9.0', 'fastapi>=0.115,<1', 'uvicorn>=0.34,<1',
    'python-multipart>=0.0.20,<1', 'pydantic>=2.10,<3', 'httpx>=0.28,<1', 'pillow>=10,<13']
  const action = existsSync(join(prefix, 'conda-meta', 'history')) ? 'install' : 'create'
  await run(conda, [action, '--yes', '--prefix', prefix, '--override-channels', '--channel', 'conda-forge', ...specs], record)
  const python = join(prefix, 'python.exe')
  await run(python, ['-c', 'import OCC.Core, fastapi, uvicorn, multipart, pydantic, httpx, PIL'], record)
  saveInstallation(config, { pythonExecutable: python })
  return python
}

// Export into a fresh directory so upgrades never overwrite user-authored code.
export async function exportDevelopmentSource(destination) {
  const target = resolve(destination)
  if (existsSync(target)) throw new Error('Choose a new directory for the development source export')
  const temporary = target + '.defeature-export'
  if (existsSync(temporary)) throw new Error('A previous export is still present at ' + temporary)
  try {
    await cp(join(packageRoot, 'runtime'), temporary, { recursive: true })
    for (const item of ['src', 'lib', 'presets', 'skills', 'environment.yml', 'package.json', 'cordis.patch.yml']) {
      await cp(join(packageRoot, item), join(temporary, 'dsh-plugins', 'defeature-ui', item), { recursive: true })
    }
    await rename(temporary, target)
  } catch (error) {
    await rm(temporary, { recursive: true, force: true })
    throw error
  }
  return target
}
