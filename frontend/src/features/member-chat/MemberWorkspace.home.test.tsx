// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MemberWorkspace from './MemberWorkspace'
import { conversationLabel } from '../layout/Sidebar'
import { makeLimits, makeMessage } from './chatQueueBuilders'
import type { OpenChatEvents } from './chatEventStream'

const auth = vi.hoisted(() => ({ value: { accessToken: 'token-a', user: { id: 'A' } } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth.value }))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  auth.value = { accessToken: 'token-a', user: { id: 'A' } }
})
const conversation = { id: '501', created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' }
const open: OpenChatEvents = ({ onEvent, signal }) => {
  onEvent({ type: 'snapshot', data: { items: [], limits: makeLimits(), server_time: new Date().toISOString() } })
  return new Promise((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
}
function setup() {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    const body =
      url === '/api/conversations?limit=20'
        ? { items: [conversation], next_cursor: null }
        : url.includes('/messages?')
          ? { items: [makeMessage({ id: '1', role: 'user', content: '보존된 질문' })], next_cursor: null }
          : { items: [], limits: makeLimits(), next_cursor: null }
    return new Response(JSON.stringify(body), { status: 200 })
  })
  const logout = vi.fn()
  const view = render(<MemberWorkspace onLogoutClick={logout} options={{ events: { open } }} />)
  return { fetchSpy, logout, ...view }
}
async function historyButton() {
  return screen.findByRole('button', { name: conversationLabel(conversation) })
}

test('회원도 환영 화면에서 시작하며 목록을 읽어도 대화를 선택·조회·생성하지 않는다', async () => {
  const { fetchSpy } = setup()
  await historyButton()
  await waitFor(() => expect(screen.queryByText('대화 기록을 불러오는 중이에요.')).toBeNull())
  expect(screen.getByRole('heading', { name: /복잡한 교통법/ })).toBeTruthy()
  expect(screen.queryByLabelText('질문 입력')).toBeNull()
  expect((await historyButton()).getAttribute('aria-current')).toBeNull()
  expect(fetchSpy.mock.calls.map(([input]) => String(input))).toEqual(['/api/conversations?limit=20'])
})

test('홈의 질문 시작은 빈 입력창만 열고 제출 전에는 대화를 생성하지 않는다', async () => {
  const user = userEvent.setup()
  const { fetchSpy } = setup()
  await historyButton()
  await user.click(screen.getByRole('button', { name: '새 질문 시작하기' }))
  expect(screen.getByLabelText('질문 입력')).toBeTruthy()
  expect(screen.getByText('교통법에 대해 질문해 보세요')).toBeTruthy()
  expect(fetchSpy.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  expect(fetchSpy.mock.calls.map(([input]) => String(input))).toEqual(['/api/conversations?limit=20'])
})

test('홈은 클릭·Enter·Space로 돌아가며 로그인·기록·작성 중 입력을 보존한다', async () => {
  const user = userEvent.setup()
  const { fetchSpy, logout } = setup()
  await user.click(await historyButton())
  await screen.findByText('보존된 질문')
  await user.type(screen.getByLabelText('질문 입력'), '작성 중인 질문')
  const home = screen.getByRole('button', { name: '교통법 AI 홈' })
  for (const key of ['click', '{Enter}', ' ']) {
    if (key === 'click') await user.click(home)
    else {
      home.focus()
      await user.keyboard(key)
    }
    expect(screen.getByRole('heading', { name: /복잡한 교통법/ })).toBeTruthy()
    expect(screen.queryByText('보존된 질문')).toBeNull()
    expect(screen.getByRole('button', { name: '로그아웃' })).toBeTruthy()
    await user.click(await historyButton())
    await screen.findByText('보존된 질문')
    expect((screen.getByLabelText('질문 입력') as HTMLTextAreaElement).value).toBe('작성 중인 질문')
  }
  expect(logout).not.toHaveBeenCalled()
  expect(fetchSpy.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
})

test('기록을 연 뒤에도 홈으로 돌아가면 대화를 자동으로 다시 열지 않는다', async () => {
  const { fetchSpy } = setup()
  fireEvent.click(await historyButton())
  await screen.findByText('보존된 질문')
  fireEvent.click(screen.getByRole('button', { name: '교통법 AI 홈' }))
  const calls = fetchSpy.mock.calls.length
  await waitFor(() => expect(screen.getByRole('heading', { name: /복잡한 교통법/ })).toBeTruthy())
  expect(fetchSpy.mock.calls).toHaveLength(calls)
  expect(screen.queryByLabelText('질문 입력')).toBeNull()
})
