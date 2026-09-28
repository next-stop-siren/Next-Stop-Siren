export type Check = 'health' | 'ready'

export async function checkApi(check: Check): Promise<string> {
  const response = await fetch(`/api/${check}`, { headers: { Accept: 'application/json' } })
  const body: unknown = await response.json()
  if (!response.ok) {
    if (typeof body === 'object' && body !== null && 'message' in body && typeof body.message === 'string') {
      throw new Error(body.message)
    }
    throw new Error(`요청이 실패했습니다 (${response.status}).`)
  }
  if (typeof body !== 'object' || body === null || !('status' in body) || typeof body.status !== 'string') {
    throw new Error('예상하지 못한 응답을 받았습니다.')
  }
  return body.status
}
