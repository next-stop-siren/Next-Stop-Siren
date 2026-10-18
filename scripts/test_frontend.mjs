import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, copyFileSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { test } from 'node:test'
import { freePort } from './frontend.mjs'

const root = resolve(import.meta.dirname, '..')
test('occupied frontend port fails and leaves its owner listening', async () => {
  const server = createServer()
  await new Promise((done) => server.listen(0, '127.0.0.1', done))
  try {
    await assert.rejects(freePort(server.address().port), /occupied or unavailable/)
    assert.equal(server.listening, true)
  } finally { await new Promise((done) => server.close(done)) }
})
test('shell frontend commands bypass Python, uv, Docker and browser installation', { skip: process.platform === 'win32' }, () => {
  const temp = mkdtempSync(resolve(tmpdir(), 'b71-frontend-'))
  try {
    mkdirSync(resolve(temp, 'scripts'))
    mkdirSync(resolve(temp, 'frontend/node_modules'), { recursive: true })
    copyFileSync(resolve(root, 'local.sh'), resolve(temp, 'local.sh'))
    copyFileSync(resolve(root, 'scripts/frontend.mjs'), resolve(temp, 'scripts/frontend.mjs'))
    writeFileSync(resolve(temp, 'bootstrap.sh'), '#!/bin/sh\n[ "$1" = --frontend ] || exit 99\n', { mode: 0o755 })
    const npm = resolve(temp, 'npm')
    writeFileSync(npm, '#!/bin/sh\nif [ "$1" = --version ]; then echo 11.19.0; else echo "$*" >> "$B71_CALLS"; fi\n', { mode: 0o755 })
    for (const command of ['frontend-setup', 'frontend-check', 'frontend-test', 'frontend-build']) {
      const result = spawnSync('/bin/sh', ['local.sh', command], { cwd: temp, encoding: 'utf8', env: {
        ...process.env, B71_NODE: process.execPath, B71_NPM: npm, B71_CALLS: resolve(temp, 'calls'),
        B71_PYTHON: '/missing/python', B71_UV: '/missing/uv', B71_DOCKER: '/missing/docker',
      } })
      assert.equal(result.status, 0, result.stderr)
    }
    assert.deepEqual(readFileSync(resolve(temp, 'calls'), 'utf8').trim().split('\n'),
      ['ci', 'run format:check', 'run lint', 'run typecheck', 'run test:unit', 'run build'])
    assert.equal(existsSync(resolve(temp, '.env')), false)
    assert.equal(existsSync(resolve(temp, 'backend')), false)
  } finally { rmSync(temp, { recursive: true, force: true }) }
})
