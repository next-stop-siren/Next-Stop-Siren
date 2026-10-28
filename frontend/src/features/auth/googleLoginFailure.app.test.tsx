// @vitest-environment jsdom
//
// Google 실패 복귀(#69) 화면 연결. 실패 query는 안내용이므로 인증 상태를 바꾸지 않고,
// 세션 복구 결과에 따라 비회원은 모달, 기존 회원은 배너로만 안내한다.
import { afterEach, expect, test } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../../App'
import { googleLoginStartUrl } from './authApi'
import { errorBody, hangUntilAborted, jsonResponse, strictFetch } from './authTestFixtures'

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

function guestFetch() {
  const calls: string[] = []
  const spy = strictFetch((url) => {
    calls.push(url)
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    return undefined
  })
  return { spy, calls }
}

function currentPath() {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`
}

test.each([
  ['cancelled', 'Google 로그인을 취소했어요', '다시 시도하거나 이메일로 로그인해 주세요.'],
  [
    'invalid_request',
    'Google 로그인에 실패했어요',
    '로그인 요청을 확인할 수 없어요. 다시 시작하거나 이메일로 로그인해 주세요.',
  ],
  [
    'temporarily_unavailable',
    'Google 로그인에 실패했어요',
    '지금은 Google 로그인을 완료할 수 없어요. 잠시 후 다시 시도하거나 이메일로 로그인해 주세요.',
  ],
])('비회원 복귀 %s: 실패 모달을 열고 주소의 실패 값을 지운다', async (code, title, description) => {
  window.history.replaceState(null, '', `/?google_login_error=${code}`)
  const { calls } = guestFetch()

  render(<App />)

  const dialog = await screen.findByRole('dialog', { name: title })
  expect(dialog.getAttribute('aria-describedby')).toBe('google-failure-description')
  expect(document.getElementById('google-failure-description')?.textContent).toBe(description)
  expect(screen.queryByText(/개발용 미리보기/)).toBeNull()

  const retry = screen.getByRole('link', { name: 'Google로 다시 시도하기' })
  expect(retry.getAttribute('href')).toBe(googleLoginStartUrl)
  expect(document.activeElement).toBe(retry)

  expect(currentPath()).toBe('/')
  // 실패 안내만으로 로그인·로그아웃 같은 인증 요청을 보내지 않는다.
  expect(calls).toEqual(['/api/auth/csrf'])
})

test('알 수 없는 값은 원문을 출력하지 않고 일반 안내로 보여준다', async () => {
  window.history.replaceState(null, '', '/?google_login_error=%3Cb%3Eprovider%20detail%3C%2Fb%3E')
  guestFetch()

  render(<App />)

  await screen.findByRole('dialog', { name: 'Google 로그인에 실패했어요' })
  expect(document.body.textContent).not.toContain('provider detail')
  expect(currentPath()).toBe('/')
})

test('실패 query가 없으면 모달을 열지 않는다', async () => {
  guestFetch()

  render(<App />)

  await screen.findByRole('button', { name: /^로그인$/ })
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('이메일로 로그인을 고르면 로그인 폼으로 넘어가 이메일 입력에 초점이 간다', async () => {
  window.history.replaceState(null, '', '/?google_login_error=cancelled')
  guestFetch()
  const user = userEvent.setup()

  render(<App />)

  await screen.findByRole('dialog', { name: 'Google 로그인을 취소했어요' })
  await user.click(screen.getByRole('button', { name: '이메일로 로그인' }))

  expect(screen.getByRole('dialog', { name: '다시 만나서 반가워요' })).toBeTruthy()
  expect(document.activeElement).toBe(screen.getByLabelText('이메일'))
})

test('실패 모달을 닫았다가 다시 열면 일반 로그인 화면이 보인다', async () => {
  window.history.replaceState(null, '', '/?google_login_error=temporarily_unavailable')
  guestFetch()
  const user = userEvent.setup()

  render(<App />)

  await screen.findByRole('dialog', { name: 'Google 로그인에 실패했어요' })
  await user.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

  await user.click(screen.getByRole('button', { name: /^로그인$/ }))
  expect(screen.getByRole('dialog', { name: '다시 만나서 반가워요' })).toBeTruthy()
})

test('기존 회원 세션이 복구되면 회원 화면을 유지하고 배너로만 안내한다', async () => {
  window.history.replaceState(null, '', '/?google_login_error=cancelled')
  const calls: string[] = []
  strictFetch((url, init) => {
    calls.push(url)
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'csrf-boot' })
    if (url === '/api/auth/refresh')
      return jsonResponse(200, {
        access_token: 'access-boot',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-boot-2',
      })
    if (url === '/api/me')
      return jsonResponse(200, { user: { id: 'u1', email: 'member@test.com', created_at: '2026-01-01T00:00:00Z' } })
    if (url === '/api/conversations?limit=20') return jsonResponse(200, { items: [], next_cursor: null })
    if (url === '/api/chat/events') return hangUntilAborted(init?.signal)
    return undefined
  })
  const user = userEvent.setup()

  render(<App />)

  const banner = await screen.findByText('Google 로그인을 취소했어요. 현재 로그인 상태는 그대로 유지돼요.')
  expect(banner.closest('[role="status"]')).toBeTruthy()
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByRole('button', { name: '로그아웃' })).toBeTruthy()
  expect(calls).not.toContain('/api/auth/logout')
  expect(currentPath()).toBe('/')

  await user.click(screen.getByRole('button', { name: '닫기' }))
  expect(screen.queryByText(/현재 로그인 상태는 그대로 유지돼요/)).toBeNull()
  expect(screen.getByRole('button', { name: '로그아웃' })).toBeTruthy()
})

test('개발용 미리보기는 실제 복귀와 구분해 표시한다', async () => {
  guestFetch()
  const user = userEvent.setup()

  render(<App />)

  await user.click(await screen.findByRole('button', { name: /^로그인$/ }))
  await user.click(screen.getByRole('button', { name: '[개발용] Google 로그인 실패 화면 보기' }))

  expect(screen.getByRole('dialog', { name: 'Google 로그인에 실패했어요' })).toBeTruthy()
  expect(screen.getByText('[개발용 미리보기] 실제 Google 로그인 복귀가 아닙니다.')).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '이메일로 로그인' }))
  expect(document.activeElement).toBe(screen.getByLabelText('이메일'))
})
