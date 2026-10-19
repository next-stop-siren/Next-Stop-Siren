// docs/04-reference/authentication.md, docs/04-reference/api.md 기준 구현.
// 이 파일은 네트워크 호출과 응답 해석만 담당한다. 화면 상태는 AuthContext가 가진다.

export type PublicUser = {
  id: string
  email: string
  created_at: string
}

export type AuthSuccessBody = {
  user: PublicUser
  access_token: string
  token_type: 'Bearer'
  expires_in: number
  csrf_token: string
}

export type CsrfBody = {
  csrf_token: string
}

export type RefreshBody = {
  access_token: string
  token_type: 'Bearer'
  expires_in: number
  csrf_token: string
}

export type ApiErrorCode =
  | 'unauthenticated'
  | 'csrf_failed'
  | 'not_found'
  | 'request_key_conflict'
  | 'conversation_busy'
  | 'refresh_race'
  | 'local_email_exists'
  | 'validation_error'
  | 'rate_limited'
  | 'provider_failed'
  | 'provider_timeout'
  | 'temporarily_unavailable'

export type ApiErrorBody = {
  error: {
    code: ApiErrorCode
    message: string
    fields: Record<string, string>
    trace_id: string
  }
}

export class AuthApiError extends Error {
  code: ApiErrorCode
  fields: Record<string, string>
  status: number

  constructor(status: number, body: ApiErrorBody) {
    super(body.error.message)
    this.name = 'AuthApiError'
    this.status = status
    this.code = body.error.code
    this.fields = body.error.fields
  }
}

// 타임아웃(또는 그 외 사유로 인한 abort)으로 요청이 끝난 경우 던진다. 호출 측
// (AuthContext)은 이걸 "이 요청은 끝내 응답을 못 받았다"는 신호로 다루면 된다 —
// AuthApiError(서버가 명시적으로 거절한 경우)와는 구분되는, 별도의 실패 유형이다.
export class AuthTimeoutError extends Error {
  constructor(url: string, timeoutMs: number) {
    super(`${url} 요청이 ${timeoutMs}ms 안에 끝나지 않았습니다.`)
    this.name = 'AuthTimeoutError'
  }
}

const DEFAULT_TIMEOUT_MS = 10_000

async function parseJsonOrThrow<T>(response: Response): Promise<T> {
  // logout은 204라 본문이 없다.
  const body: unknown = response.status === 204 ? null : await response.json()

  if (!response.ok) {
    if (isApiErrorBody(body)) {
      throw new AuthApiError(response.status, body)
    }
    throw new Error(`요청이 실패했습니다 (${response.status}).`)
  }

  return body as T
}

function isApiErrorBody(body: unknown): body is ApiErrorBody {
  return (
    typeof body === 'object' && body !== null && 'error' in body && typeof (body as ApiErrorBody).error === 'object'
  )
}

// fetch + 응답 파싱을 하나의 AbortController/타임아웃으로 묶는다. 헤더만 늦는
// 경우뿐 아니라, 헤더는 왔지만 body 스트림이 끝나지 않는 경우(parseJsonOrThrow의
// response.json() 단계)도 같은 signal로 커버된다 — abort되면 아직 읽고 있던
// body 스트림도 함께 끊어지기 때문이다. 요청 옵션(credentials, headers 등)은
// 그대로 전달하고, signal만 덧붙인다.
async function requestJson<T>(url: string, init: RequestInit, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const response = await fetch(url, { ...init, signal: controller.signal })
    return await parseJsonOrThrow<T>(response)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new AuthTimeoutError(url, timeoutMs)
    }
    // fetch 자체가 지원하지 않는 환경이거나 polyfill이 AbortError를 DOMException이
    // 아닌 평범한 Error(name: 'AbortError')로 던지는 경우도 있어 이름만으로도 한 번 더 본다.
    if (error instanceof Error && error.name === 'AbortError') {
      throw new AuthTimeoutError(url, timeoutMs)
    }
    throw error
  } finally {
    clearTimeout(timeoutId)
  }
}

// 로컬 이메일·비밀번호 가입. 브라우저가 Origin 헤더를 자동으로 붙인다.
export async function register(email: string, password: string): Promise<AuthSuccessBody> {
  return requestJson<AuthSuccessBody>('/api/auth/register', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

// 로컬 이메일·비밀번호 로그인.
export async function login(email: string, password: string): Promise<AuthSuccessBody> {
  return requestJson<AuthSuccessBody>('/api/auth/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

// refresh 쿠키만으로는 access를 못 받는다. CSRF 값을 먼저 읽어야 한다.
export async function fetchCsrf(): Promise<CsrfBody> {
  return requestJson<CsrfBody>('/api/auth/csrf', {
    method: 'GET',
    credentials: 'include',
  })
}

// 403/409의 1회 재시도는 이 함수를 호출하는 쪽(AuthContext)의 책임으로 둔다.
// 여기서는 계약대로 "정확히 한 번의 POST"만 수행한다.
export async function refresh(csrfToken: string): Promise<RefreshBody> {
  return requestJson<RefreshBody>('/api/auth/refresh', {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRF-Token': csrfToken },
  })
}

export async function logout(csrfToken: string): Promise<void> {
  await requestJson<null>('/api/auth/logout', {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRF-Token': csrfToken },
  })
}

export async function getMe(accessToken: string): Promise<{ user: PublicUser }> {
  return requestJson<{ user: PublicUser }>('/api/me', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
}

// Google은 팝업이 아니라 전체 페이지 이동이다. fetch로 호출하지 않는다.
export const googleLoginStartUrl = '/api/auth/google/start'
