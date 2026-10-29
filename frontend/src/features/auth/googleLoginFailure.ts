// Google 로그인 실패 복귀(#69)의 화면 쪽 처리.
// 서버는 실패 시 `/?google_login_error=<고정 값>`으로 302 복귀한다. 이 값은 안내용일
// 뿐이며 URL을 손으로 바꿀 수 있으므로 인증 성공·로그아웃·계정 전환의 근거로 쓰지
// 않는다. 실제 인증 상태는 AuthContext의 csrf→refresh→me 복구 결과로만 판단한다.

export const GOOGLE_LOGIN_ERROR_PARAM = 'google_login_error'

const KNOWN_CODES = ['cancelled', 'invalid_request', 'temporarily_unavailable'] as const

type KnownGoogleLoginErrorCode = (typeof KNOWN_CODES)[number]

// 'unknown'은 빈 값·알 수 없는 값·중복 값을 묶은 화면 전용 분류다. 원문은 보관하지 않는다.
export type GoogleLoginFailureKind = KnownGoogleLoginErrorCode | 'unknown'

export type GoogleLoginFailureCopy = {
  title: string
  description: string
}

const FAILURE_COPY: Record<GoogleLoginFailureKind, GoogleLoginFailureCopy> = {
  cancelled: {
    title: 'Google 로그인을 취소했어요',
    description: '다시 시도하거나 이메일로 로그인해 주세요.',
  },
  invalid_request: {
    title: 'Google 로그인에 실패했어요',
    description: '로그인 요청을 확인할 수 없어요. 다시 시작하거나 이메일로 로그인해 주세요.',
  },
  temporarily_unavailable: {
    title: 'Google 로그인에 실패했어요',
    description: '지금은 Google 로그인을 완료할 수 없어요. 잠시 후 다시 시도하거나 이메일로 로그인해 주세요.',
  },
  unknown: {
    title: 'Google 로그인에 실패했어요',
    description: '로그인을 완료하지 못했어요. 다시 시도하거나 이메일로 로그인해 주세요.',
  },
}

function isKnownCode(value: string): value is KnownGoogleLoginErrorCode {
  return (KNOWN_CODES as readonly string[]).includes(value)
}

// 실패 query가 없으면 null. 값이 정확히 하나이고 고정 코드일 때만 그 코드로 해석한다.
export function readGoogleLoginFailure(search: string): GoogleLoginFailureKind | null {
  const values = new URLSearchParams(search).getAll(GOOGLE_LOGIN_ERROR_PARAM)
  if (values.length === 0) return null
  if (values.length === 1 && isKnownCode(values[0])) return values[0]
  return 'unknown'
}

export function googleLoginFailureCopy(kind: GoogleLoginFailureKind): GoogleLoginFailureCopy {
  return FAILURE_COPY[kind]
}

// 처리한 실패 값을 주소에서 지워 새로고침·뒤로 가기 뒤 같은 안내가 반복되지 않게 한다.
// 다른 query·hash와 history state는 그대로 둔다. 여러 번 불러도 결과가 같다.
export function consumeGoogleLoginFailureParam(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has(GOOGLE_LOGIN_ERROR_PARAM)) return
  url.searchParams.delete(GOOGLE_LOGIN_ERROR_PARAM)
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}
