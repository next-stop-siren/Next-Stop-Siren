// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { conversationLabel } from '../layout/Sidebar'
import MemberWorkspace from './MemberWorkspace'
import { makeEnvelope, makeLimits, makeMessage } from './chatQueueBuilders'
import type { ApiConversation } from './memberChatApiTypes'

const auth = vi.hoisted(() => ({ value: { accessToken: 'token-A', user: { id: 'A' } } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth.value }))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  auth.value = { accessToken: 'token-A', user: { id: 'A' } }
})
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
function conversation(id: number): ApiConversation {
  const date = new Date(Date.UTC(2026, 10, 1, 0, id)).toISOString()
  return { id: String(id), created_at: date, updated_at: date }
}
function renderWorkspace() {
  const ui = () => (
    <MemberWorkspace
      key={auth.value.user.id}
      onLogoutClick={() => {}}
      options={{
        events: {
          open: ({ signal }) =>
            new Promise((resolve) => signal.addEventListener('abort', () => resolve(), { once: true })),
        },
      }}
    />
  )
  const view = render(ui())
  return { ...view, rerender: () => view.rerender(ui()) }
}
const history = () => within(screen.getByRole('navigation', { name: '대화 기록' }))
function listPage(before: number, total: number) {
  const newest = Math.min(before - 1, total)
  const items = Array.from({ length: Math.min(newest, 20) }, (_, i) => conversation(newest - i))
  return { items, next_cursor: newest > 20 ? items.at(-1)!.id : null }
}

test('일반 홈 사이드바에서 1,100개 대화를 페이지별로 요청해 마지막 기록까지 선택한다', async () => {
  const calls: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'https://fixture.invalid')
    calls.push(url.pathname + url.search)
    if (url.pathname === '/api/conversations')
      return json(listPage(Number(url.searchParams.get('before_id') ?? 1101), 1100))
    if (url.pathname.endsWith('/messages'))
      return json({
        items: [makeMessage({ id: '1', role: 'user', content: '가장 오래된 대화 질문' })],
        next_cursor: null,
      })
    return json({ items: [], next_cursor: null, limits: makeLimits() })
  })
  renderWorkspace()
  await screen.findByRole('button', { name: conversationLabel(conversation(1100)) })
  expect(history().getAllByRole('button', { name: /대화 ·/ })).toHaveLength(20)
  expect(calls).toHaveLength(1)
  const more = screen.getByRole('button', { name: '기록 더 불러오기' })
  for (let page = 2; page <= 55; page++) {
    fireEvent.click(more)
    await waitFor(() =>
      expect(document.querySelectorAll('nav[aria-label="대화 기록"] ul button')).toHaveLength(page * 20),
    )
  }
  expect(screen.queryByRole('button', { name: '기록 더 불러오기' })).toBeNull()
  expect(calls.filter((url) => url.startsWith('/api/conversations?'))).toHaveLength(55)
  expect(screen.queryByLabelText('질문 입력')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: conversationLabel(conversation(1)) }))
  expect(await screen.findByText('가장 오래된 대화 질문')).toBeTruthy()
}, 30000)

test('뒤 페이지 실패는 기존 목록을 유지하고 같은 커서로 다시 불러온다', async () => {
  const cursors: (string | null)[] = []
  let failed = false
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'https://fixture.invalid')
    const cursor = url.searchParams.get('before_id')
    cursors.push(cursor)
    if (cursor && !failed) {
      failed = true
      return json({ error: { message: '페이지 일시 오류' } }, 503)
    }
    return json(listPage(Number(cursor ?? 41), 40))
  })
  renderWorkspace()
  await screen.findByRole('button', { name: conversationLabel(conversation(40)) })
  fireEvent.click(screen.getByRole('button', { name: '기록 더 불러오기' }))
  await screen.findByText('페이지 일시 오류')
  expect(history().getAllByRole('button', { name: /대화 ·/ })).toHaveLength(20)
  fireEvent.click(screen.getByRole('button', { name: '기록 다시 불러오기' }))
  await waitFor(() => expect(history().getAllByRole('button', { name: /대화 ·/ })).toHaveLength(40))
  expect(cursors).toEqual([null, '21', '21'])
  expect(screen.queryByText('페이지 일시 오류')).toBeNull()
})

test('중복 클릭은 한 요청만 보내고, 회원 전환은 이전 페이지를 중단해 늦은 목록이 섞이지 않는다', async () => {
  let resolve!: (response: Response) => void
  let lateSignal: AbortSignal | null | undefined
  const pending = new Promise<Response>((done) => {
    resolve = done
  })
  let nextCalls = 0
  const seen: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = new URL(String(input), 'https://fixture.invalid')
    const memberB = new Headers(init?.headers).get('Authorization') === 'Bearer token-B'
    seen.push(`${memberB ? 'B' : 'A'}:${url.searchParams.get('before_id') ?? 'first'}`)
    if (memberB) return json({ items: [conversation(901)], next_cursor: null })
    if (url.searchParams.has('before_id')) {
      nextCalls++
      lateSignal = init?.signal
      return pending
    }
    return json(listPage(41, 40))
  })
  const ui = renderWorkspace()
  await screen.findByRole('button', { name: conversationLabel(conversation(40)) })
  const button = screen.getByRole('button', { name: '기록 더 불러오기' })
  act(() => {
    fireEvent.click(button)
    fireEvent.click(button)
  })
  expect(nextCalls).toBe(1)
  auth.value = { accessToken: 'token-B', user: { id: 'B' } }
  ui.rerender()
  await screen.findByRole('button', { name: conversationLabel(conversation(901)) })
  expect(lateSignal?.aborted).toBe(true)
  await act(async () => {
    resolve(json(listPage(21, 40)))
    await pending
  })
  expect(history().getAllByRole('button', { name: /대화 ·/ })).toHaveLength(1)
  expect(seen).toEqual(['A:first', 'A:21', 'B:first'])
  expect(screen.queryByRole('button', { name: conversationLabel(conversation(1)) })).toBeNull()
})

test('5,500개 메시지의 일반 기록 재진입은 추가 클릭 없이 최신 답변과 실패 동작을 복구한다', async () => {
  const latest = makeEnvelope({
    job: { id: '2750', question_message_id: '5499', state: 'failed', allowed_actions: ['retry', 'skip'] },
    question: { content: '최신 실패 질문' },
    answer: { id: '5500', status: 'failed' },
  })
  const messages = Array.from({ length: 2749 }, (_, i) => [
    makeMessage({ id: String(i * 2 + 1), role: 'user', content: `저장 질문 ${i}` }),
    makeMessage({
      id: String(i * 2 + 2),
      role: 'assistant',
      reply_to_message_id: String(i * 2 + 1),
      content: i === 2748 ? '최신 저장 답변' : `저장 답변 ${i}`,
    }),
  ])
    .flat()
    .concat([latest.question, latest.answer!])
  let pages = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = new URL(String(input), 'https://fixture.invalid')
    if (url.pathname === '/api/conversations') return json({ items: [conversation(501)], next_cursor: null })
    if (url.pathname.endsWith('/messages')) {
      pages++
      const start = Number(url.searchParams.get('after_id') ?? 0)
      const items = messages.slice(start, start + 100)
      return json({ items, next_cursor: start + 100 < messages.length ? items.at(-1)!.id : null })
    }
    return json({ items: [latest.job], next_cursor: null, limits: makeLimits() })
  })
  renderWorkspace()
  fireEvent.click(await screen.findByRole('button', { name: conversationLabel(conversation(501)) }))
  expect(await screen.findByText('최신 저장 답변', {}, { timeout: 15000 })).toBeTruthy()
  expect(screen.getByText('최신 실패 질문')).toBeTruthy()
  expect(screen.getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '건너뛰기' })).toBeTruthy()
  expect(pages).toBe(55)
}, 30000)
