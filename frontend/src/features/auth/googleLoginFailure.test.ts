// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest'
import { consumeGoogleLoginFailureParam, googleLoginFailureCopy, readGoogleLoginFailure } from './googleLoginFailure'

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

test.each([
  ['?google_login_error=cancelled', 'cancelled'],
  ['?google_login_error=invalid_request', 'invalid_request'],
  ['?google_login_error=temporarily_unavailable', 'temporarily_unavailable'],
] as const)('고정 코드 %s는 그대로 해석한다', (search, expected) => {
  expect(readGoogleLoginFailure(search)).toBe(expected)
})

test('실패 query가 없으면 안내하지 않는다', () => {
  expect(readGoogleLoginFailure('')).toBeNull()
  expect(readGoogleLoginFailure('?chatMock=1')).toBeNull()
})

test.each([
  '?google_login_error=',
  '?google_login_error',
  '?google_login_error=access_denied',
  '?google_login_error=CANCELLED',
  '?google_login_error=%3Cscript%3E',
  '?google_login_error=cancelled&google_login_error=cancelled',
  '?google_login_error=cancelled&google_login_error=invalid_request',
])('빈·알 수 없는·중복 값 %s는 원문 없이 일반 안내로 묶는다', (search) => {
  expect(readGoogleLoginFailure(search)).toBe('unknown')
})

test('안내 문구는 고정 코드에서만 만들어진다', () => {
  expect(googleLoginFailureCopy('cancelled').title).toBe('Google 로그인을 취소했어요')
  expect(googleLoginFailureCopy('unknown').title).toBe('Google 로그인에 실패했어요')
})

test('실패 값만 주소에서 지우고 다른 query·hash·history state는 유지한다', () => {
  window.history.replaceState({ keep: true }, '', '/?a=1&google_login_error=cancelled&google_login_error=x&b=2#top')
  const lengthBefore = window.history.length

  consumeGoogleLoginFailureParam()

  expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe('/?a=1&b=2#top')
  expect(window.history.state).toEqual({ keep: true })
  expect(window.history.length).toBe(lengthBefore)
})

test('실패 값만 있던 주소는 query 없이 정리되고 다시 불러도 그대로다', () => {
  window.history.replaceState(null, '', '/?google_login_error=cancelled')
  consumeGoogleLoginFailureParam()
  consumeGoogleLoginFailureParam()
  expect(`${window.location.pathname}${window.location.search}`).toBe('/')
})
