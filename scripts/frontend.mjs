/** Frontend-only commands. Node stdlib; no Python, backend, .env or Docker access. */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { dirname, delimiter, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const frontend = resolve(root, 'frontend')
const commands = ['frontend-setup', 'frontend-dev', 'frontend-check', 'frontend-test', 'frontend-build']
const env = { ...process.env, PATH: `${dirname(process.execPath)}${delimiter}${process.env.PATH ?? ''}` }
// Invoke npm's JS entry directly, avoiding Windows .cmd shell interpolation.
const npm = process.env.B71_NPM || (process.platform === 'win32'
  ? resolve(dirname(process.execPath), 'npm.cmd') : resolve(dirname(process.execPath), 'npm'))
function npmCommand(args) {
  if (process.platform === 'win32' && npm.endsWith('.cmd')) {
    return [process.execPath, [resolve(dirname(npm), 'node_modules/npm/bin/npm-cli.js'), ...args]]
  }
  return [npm, args]
}
function run(args, capture = false) {
  const [program, argv] = npmCommand(args)
  const result = spawnSync(program, argv, { cwd: frontend, env, encoding: 'utf8', stdio: capture ? 'pipe' : 'inherit' })
  if (result.error || result.status !== 0) throw new Error(result.error?.message || `npm ${args.join(' ')} failed (${result.status})`)
  return result.stdout?.trim()
}
export function freePort(port = 5173) {
  return new Promise((accept, reject) => {
    const server = createServer()
    server.once('error', () => reject(new Error(`Local port ${port} is occupied or unavailable; frontend was not started. Stop its owner manually.`)))
    server.listen(port, '127.0.0.1', () => server.close(accept))
  })
}
async function dev() {
  await freePort()
  const [program, argv] = npmCommand(['run', 'dev', '--', '--port', '5173', '--strictPort'])
  const child = spawn(program, argv, { cwd: frontend, env, stdio: 'inherit', detached: process.platform !== 'win32' })
  let stopping = false
  function stop() {
    if (stopping) return
    stopping = true
    if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
    else { try { process.kill(-child.pid, 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error } }
  }
  process.on('SIGINT', stop)
  process.on('SIGTERM', stop)
  await new Promise((accept, reject) => {
    child.once('error', reject)
    child.once('exit', (code) => stopping || code === 0 ? accept() : reject(new Error(`Frontend dev exited (${code})`)))
  }).finally(() => { process.off('SIGINT', stop); process.off('SIGTERM', stop) })
}
export async function main(command) {
  if (!commands.includes(command)) throw new Error(`Use: ${commands.join(', ')}`)
  if (process.version !== 'v24.21.0') throw new Error(`Node v24.21.0 required; found ${process.version}`)
  const version = run(['--version'], true)
  if (version !== '11.19.0') throw new Error(`npm 11.19.0 required; found ${version}`)
  if (command === 'frontend-setup') { run(['ci']); return }
  if (!existsSync(resolve(frontend, 'node_modules'))) throw new Error('Run frontend-setup first')
  if (command === 'frontend-dev') { await dev(); return }
  const tasks = command === 'frontend-check' ? ['format:check', 'lint', 'typecheck']
    : command === 'frontend-test' ? ['test:unit'] : ['build']
  for (const task of tasks) run(['run', task])
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv[2]).catch((error) => { console.error(`error: ${error.message}`); process.exitCode = 2 })
}
