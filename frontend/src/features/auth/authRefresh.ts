import { AuthApiError, fetchCsrf, refresh } from './authApi'

// refresh POST는 최초 요청과 재시도 한 번을 합쳐 최대 두 번이다.
export async function refreshWithRetry(knownCsrfToken?: string, isActive: () => boolean = () => true) {
  const assertActive = () => {
    if (!isActive()) throw new Error('종료된 인증 세션의 갱신입니다.')
  }
  assertActive()
  const firstToken = knownCsrfToken ?? (await fetchCsrf()).csrf_token
  assertActive()
  try {
    return await refresh(firstToken)
  } catch (error) {
    const shouldRetryOnce = error instanceof AuthApiError && (error.status === 403 || error.status === 409)
    if (!shouldRetryOnce) throw error

    assertActive()
    await new Promise((resolve) => setTimeout(resolve, 1000))
    assertActive()
    const second = await fetchCsrf()
    assertActive()
    return await refresh(second.csrf_token)
  }
}
