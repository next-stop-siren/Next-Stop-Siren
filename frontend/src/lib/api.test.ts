import { afterEach, expect, test, vi } from 'vitest'
import { checkApi } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

function respond(body: unknown, status = 200) {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

test.each([
  ['health', 'ok'],
  ['ready', 'ready'],
] as const)('returns %s status through the real helper', async (check, status) => {
  const fetch = respond({ status })
  await expect(checkApi(check)).resolves.toBe(status)
  expect(fetch).toHaveBeenCalledExactlyOnceWith(`/api/${check}`, { headers: { Accept: 'application/json' } })
})

test.each([null, [], {}, { status: 42 }, 'ready'])('rejects malformed success body %j', async (body) => {
  respond(body)
  await expect(checkApi('ready')).rejects.toThrow('예상하지 못한 응답을 받았습니다.')
})

test('rejects a 503 with the API error message', async () => {
  respond({ code: 'database_unavailable', message: '데이터베이스에 연결할 수 없습니다.' }, 503)
  await expect(checkApi('ready')).rejects.toThrow('데이터베이스에 연결할 수 없습니다.')
})

test.each([null, {}, { message: 42 }])('uses the HTTP status when an error message is absent: %j', async (body) => {
  respond(body, 502)
  await expect(checkApi('health')).rejects.toThrow('요청이 실패했습니다 (502).')
})

test('propagates a network failure', async () => {
  const failure = new TypeError('fixture network failure')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(failure))
  await expect(checkApi('ready')).rejects.toBe(failure)
})

test('rejects malformed JSON instead of reporting ready', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not-json', { status: 200 })))
  await expect(checkApi('ready')).rejects.toBeInstanceOf(SyntaxError)
})
