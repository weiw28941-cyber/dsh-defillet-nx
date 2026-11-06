import { exportDevelopmentSource } from './installation.js'

if (!process.argv[2]) {
  console.error('Usage: node lib/export-source.js <new-directory>')
  process.exitCode = 1
} else {
  console.log(await exportDevelopmentSource(process.argv[2]))
}
