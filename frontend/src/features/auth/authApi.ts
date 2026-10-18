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

// 로컬 이메일·비밀번호 가입. 브라우저가 Origin 헤더를 자동으로 붙인다.
export async function register(email: string, password: string): Promise<AuthSuccessBody> {
  const response = await fetch('/api/auth/register', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  return parseJsonOrThrow<AuthSuccessBody>(response)
}

// 로컬 이메일·비밀번호 로그인.
export async function login(email: string, password: string): Promise<AuthSuccessBody> {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  return parseJsonOrThrow<AuthSuccessBody>(response)
}

// refresh 쿠키만으로는 access를 못 받는다. CSRF 값을 먼저 읽어야 한다.
export async function fetchCsrf(): Promise<CsrfBody> {
  const response = await fetch('/api/auth/csrf', {
    method: 'GET',
    credentials: 'include',
  })
  return parseJsonOrThrow<CsrfBody>(response)
}

// 403/409의 1회 재시도는 이 함수를 호출하는 쪽(AuthContext)의 책임으로 둔다.
// 여기서는 계약대로 "정확히 한 번의 POST"만 수행한다.
export async function refresh(csrfToken: string): Promise<RefreshBody> {
  const response = await fetch('/api/auth/refresh', {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRF-Token': csrfToken },
  })
  return parseJsonOrThrow<RefreshBody>(response)
}

export async function logout(csrfToken: string): Promise<void> {
  const response = await fetch('/api/auth/logout', {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRF-Token': csrfToken },
  })
  await parseJsonOrThrow<null>(response)
}

export async function getMe(accessToken: string): Promise<{ user: PublicUser }> {
  const response = await fetch('/api/me', {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  return parseJsonOrThrow<{ user: PublicUser }>(response)
}

// Google은 팝업이 아니라 전체 페이지 이동이다. fetch로 호출하지 않는다.
export const googleLoginStartUrl = '/api/auth/google/start'
