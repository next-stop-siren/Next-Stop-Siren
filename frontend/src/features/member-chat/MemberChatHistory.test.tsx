// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { listAllConversationJobs, listConversationJobs } from './chatQueueApi'
import type { ChatEvent, ChatJob, ChatJobPage } from './chatQueueTypes'
import { makeEnvelope, makeJob, makeLimits, makeMessage } from './chatQueueBuilders'
import type { ApiMessage } from './memberChatApiTypes'
import { listAllMessages } from './memberChatApi'
import MemberWorkspace from './MemberWorkspace'
import { buildThreads, JOBS_LOAD_ERROR } from './useMemberChat'

const auth = vi.hoisted(() => ({
  value: { accessToken: 'token-A', user: { id: 'A' } },
}))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth.value }))

beforeEach(() => {
  auth.value = { accessToken: 'token-A', user: { id: 'A' } }
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function jobsPage(items: ChatJob[], cursor: string | null = null): ChatJobPage {
  return {
    items,
    next_cursor: cursor,
    limits: makeLimits(),
    server_time: '2026-10-20T03:00:00Z',
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function longHistory(state: ChatJob['state'] = 'failed') {
  const target = makeEnvelope({
    job: {
      id: '7001',
      question_message_id: '9001',
      state,
      position: null,
      error_code: state === 'interrupted' ? 'worker_interrupted' : 'provider_failed',
      allowed_actions: ['retry', 'skip'],
    },
    question: { content: '이전 질문' },
    answer: { id: '9002', status: state === 'interrupted' ? 'interrupted' : 'failed' },
  })
  const recent = Array.from({ length: 100 }, (_, index) =>
    makeEnvelope({
      job: {
        id: String(7002 + index),
        question_message_id: String(9003 + index * 2),
        state: 'completed',
        position: null,
        allowed_actions: [],
      },
      question: { content: `저장된 질문 ${index + 1}` },
      answer: { id: String(9004 + index * 2), status: 'completed', content: `저장된 답변 ${index + 1}` },
    }),
  )
  const history = [target, ...recent].flatMap((envelope) => [envelope.question, envelope.answer!])
  return { target, history, newest: recent.map((envelope) => envelope.job).reverse() }
}

function historyPage(history: ApiMessage[], cursor: string | null) {
  const start = cursor ? history.findIndex((message) => message.id === cursor) + 1 : 0
  const items = history.slice(start, start + 100)
  const last = items[items.length - 1]
  return { items, next_cursor: start + items.length < history.length ? last?.id : null }
}

function renderWorkspace() {
  const connections: { emit: (event: ChatEvent) => void }[] = []
  const ui = () => (
    <MemberWorkspace
      key={auth.value.user.id}
      onLogoutClick={() => {}}
      options={{
        events: {
          open: ({ signal, onEvent }) =>
            new Promise<void>((resolve) => {
              connections.push({ emit: (event) => act(() => onEvent(event)) })
              signal.addEventListener('abort', () => resolve(), { once: true })
            }),
        },
      }}
    />
  )
  const result = render(ui())
  return { ...result, connections, rerender: () => result.rerender(ui()) }
}

test('작업 한 페이지 API는 기존 limit와 네 번째 signal을 유지하고 before_id를 보낼 수 있다', async () => {
  const signal = new AbortController().signal
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(jobsPage([])))
  await listConversationJobs('token', '501', 20, signal, '7002')
  expect(fetchSpy.mock.calls[0]?.[0]).toBe('/api/chat/jobs?conversation_id=501&limit=20&before_id=7002')
  expect(fetchSpy.mock.calls[0]?.[1]?.signal).toBe(signal)
})

test('첫 100개 이후 작업을 커서로 읽고 마지막 페이지에서 끝내며 최신 한도를 돌려준다', async () => {
  const data = longHistory('interrupted')
  const last = { ...jobsPage([data.target.job]), limits: makeLimits({ next_attempt_at: '2026-10-20T03:00:10Z' }) }
  const fetchSpy = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(json(jobsPage(data.newest, '7002')))
    .mockResolvedValueOnce(json(last))
  const result = await listAllConversationJobs('token', '501')
  expect(result.items).toHaveLength(101)
  expect(result.items[100]).toEqual(data.target.job)
  expect(result.limits).toEqual(last.limits)
  expect(fetchSpy.mock.calls.map(([path]) => path)).toEqual([
    '/api/chat/jobs?conversation_id=501&limit=100',
    '/api/chat/jobs?conversation_id=501&limit=100&before_id=7002',
  ])
  expect(fetchSpy.mock.calls.map(([, init]) => new Headers(init?.headers).get('Authorization'))).toEqual([
    'Bearer token',
    'Bearer token',
  ])
})

test('빈 마지막 페이지에서도 추가 요청 없이 끝낸다', async () => {
  const fetchSpy = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(json(jobsPage([makeJob()], '7001')))
    .mockResolvedValueOnce(json(jobsPage([])))
  expect((await listAllConversationJobs('token', '501')).items).toHaveLength(1)
  expect(fetchSpy).toHaveBeenCalledTimes(2)
})

test.each(['반복 커서', '증가 커서', '숫자가 아닌 커서', '빈 페이지의 다음 커서'])(
  '%s는 일부 이력을 성공으로 반환하지 않고 중단한다',
  async (failure) => {
    const first = jobsPage([makeJob()], '7001')
    const next = {
      '반복 커서': first,
      '증가 커서': jobsPage([makeJob()], '7002'),
      '숫자가 아닌 커서': jobsPage([makeJob()], 'cursor'),
      '빈 페이지의 다음 커서': jobsPage([], '7000'),
    }[failure]!
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(first)).mockResolvedValueOnce(json(next))
    await expect(listAllConversationJobs('token', '501')).rejects.toThrow('다음 페이지')
    expect(fetchSpy).toHaveBeenCalledTimes(2)
  },
)

test('5,000개를 넘는 작업도 감소 커서를 따라 마지막 실패 작업까지 읽는다', async () => {
  let page = 0
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    const id = 10000 - page * 100
    page += 1
    if (page === 51)
      return json(jobsPage([makeJob({ id: String(id), state: 'failed', allowed_actions: ['retry', 'skip'] })]))
    const jobs = Array.from({ length: 100 }, (_, index) => makeJob({ id: String(id - index) }))
    return json(jobsPage(jobs, String(id - 99)))
  })
  const result = await listAllConversationJobs('token', '501')
  expect(result.items).toHaveLength(5001)
  expect(result.items.at(-1)?.allowed_actions).toEqual(['retry', 'skip'])
  expect(fetchSpy).toHaveBeenCalledTimes(51)
})

test('실패 질문의 메시지 조인도 5,000개 이후까지 읽고 마지막 빈 페이지에서 끝낸다', async () => {
  let page = 0
  const recovered = makeEnvelope({
    job: { id: '7001', question_message_id: '5001', state: 'interrupted', allowed_actions: ['retry', 'skip'] },
    answer: { id: '5002', status: 'interrupted' },
  })
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    const firstId = page * 100 + 1
    page += 1
    if (page === 52) return json({ items: [], next_cursor: null })
    const items =
      page === 51
        ? [recovered.question, recovered.answer!]
        : Array.from({ length: 100 }, (_, index) => makeMessage({ id: String(firstId + index), role: 'user' }))
    return json({ items, next_cursor: items.at(-1)?.id })
  })
  const result = await listAllMessages('token', '501')
  expect(result).toHaveLength(5002)
  expect(result.at(-1)?.id).toBe('5002')
  expect(fetchSpy.mock.calls[50]?.[0]).toBe('/api/conversations/501/messages?limit=100&after_id=5000')
  expect(fetchSpy).toHaveBeenCalledTimes(52)
  const threads = buildThreads('501', result, { [recovered.job.id]: recovered })
  expect(threads.at(-1)?.jobs).toEqual([recovered])
})

test('뒤 페이지를 읽는 중 중단되면 이후 커서를 요청하지 않는다', async () => {
  const controller = new AbortController()
  const fetchSpy = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(json(jobsPage([makeJob()], '7001')))
    .mockImplementationOnce(async () => {
      controller.abort()
      return json(jobsPage([makeJob({ id: '7000' })], '7000'))
    })
  await expect(listAllConversationJobs('token', '501', controller.signal)).rejects.toThrow()
  expect(fetchSpy).toHaveBeenCalledTimes(2)
  expect(fetchSpy.mock.calls.every(([, init]) => init?.signal === controller.signal)).toBe(true)
})

test.each(['7001', '7000', 'cursor'])('메시지 조인 조회의 반복·감소·잘못된 커서 %s를 거부한다', async (cursor) => {
  const fetchSpy = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(json({ items: [makeMessage({ id: '7001', role: 'user' })], next_cursor: '7001' }))
    .mockResolvedValueOnce(json({ items: [makeMessage({ id: '7002', role: 'user' })], next_cursor: cursor }))
  await expect(listAllMessages('token', '501')).rejects.toThrow('다음 페이지')
  expect(fetchSpy).toHaveBeenCalledTimes(2)
})

test.each(['failed', 'interrupted'] as const)(
  '새로 연 긴 대화의 101번째 %s 작업에서 재시도·건너뛰기와 같은 retry_key를 복구한다',
  async (state) => {
    const data = longHistory(state)
    let retries = 0
    let skipped = false
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = new URL(String(input), 'http://test')
      if (path.pathname === '/api/conversations') {
        return json({
          items: [{ id: '501', created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' }],
          next_cursor: null,
        })
      }
      if (path.pathname === '/api/conversations/501/messages')
        return json(historyPage(data.history, path.searchParams.get('after_id')))
      if (path.pathname === '/api/chat/jobs') {
        return json(path.searchParams.has('before_id') ? jobsPage([data.target.job]) : jobsPage(data.newest, '7002'))
      }
      if (path.pathname === '/api/conversations/501/messages/9001/retry') {
        retries += 1
        if (retries === 1) throw new TypeError('Response lost')
        return json(
          {
            job: makeJob({ id: '7102', kind: 'retry', question_message_id: '9001', state: 'queued' }),
            question: data.target.question,
            answer: null,
            limits: makeLimits(),
          },
          202,
        )
      }
      if (path.pathname === '/api/conversations/501/messages/9001/skip') {
        skipped = true
        return json({ skipped_message_id: '9001', resumed_jobs: [], limits: makeLimits() })
      }
      if (path.pathname === '/api/chat/jobs/7001') {
        return json({
          ...data.target,
          job: { ...data.target.job, allowed_actions: skipped ? ['retry'] : ['retry', 'skip'] },
        })
      }
      throw new Error(`Unexpected request ${String(input)} ${init?.method}`)
    })
    const ui = renderWorkspace()
    // Explicit selection also exercises the member-home entry point when it is integrated.
    await userEvent.click(await screen.findByRole('button', { name: /대화 ·/ }))
    expect(await screen.findByRole('button', { name: '재시도' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '건너뛰기' })).toBeTruthy()
    expect(screen.getByText('저장된 답변 100')).toBeTruthy()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '건너뛰기' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull())
    await user.click(screen.getByRole('button', { name: '재시도' }))
    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: '재시도' }))
    await waitFor(() => expect(retries).toBe(2))
    const retryCalls = fetchSpy.mock.calls.filter(([path]) => String(path).endsWith('/retry'))
    expect(JSON.parse(String(retryCalls[0]?.[1]?.body)).retry_key).toBe(
      JSON.parse(String(retryCalls[1]?.[1]?.body)).retry_key,
    )
    expect(
      fetchSpy.mock.calls.some(
        ([path]) => String(path) === '/api/chat/jobs?conversation_id=501&limit=100&before_id=7002',
      ),
    ).toBe(true)

    ui.connections
      .at(-1)
      ?.emit({ type: 'job', data: { ...data.target, job: { ...data.target.job, allowed_actions: [] } } })
    expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull()
    ui.unmount()
  },
)

test('뒤 페이지 실패는 다시 불러오기 안내로 이어지고, 재조회로 종결 작업을 복구한다', async () => {
  const data = longHistory()
  let fail = true
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const path = new URL(String(input), 'http://test')
    if (path.pathname === '/api/conversations')
      return json({
        items: [{ id: '501', created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' }],
        next_cursor: null,
      })
    if (path.pathname === '/api/conversations/501/messages')
      return json(historyPage(data.history, path.searchParams.get('after_id')))
    if (path.pathname === '/api/chat/jobs') {
      if (path.searchParams.has('before_id')) {
        return fail ? json({ error: { message: '일시 오류' } }, 503) : json(jobsPage([data.target.job]))
      }
      return json(jobsPage(data.newest, '7002'))
    }
    throw new Error(`Unexpected request ${String(input)}`)
  })
  renderWorkspace()
  await userEvent.click(await screen.findByRole('button', { name: /대화 ·/ }))
  await screen.findByText(JOBS_LOAD_ERROR)
  expect(screen.queryByRole('button', { name: '재시도' })).toBeNull()
  fail = false
  fireEvent.click(screen.getByRole('button', { name: '대화 다시 불러오기' }))
  expect(await screen.findByRole('button', { name: '재시도' })).toBeTruthy()
  expect(screen.queryByText(JOBS_LOAD_ERROR)).toBeNull()
})

test('회원 전환 뒤 이전 회원의 늦은 뒤 페이지는 작업·버튼을 새 화면에 섞지 않는다', async () => {
  const data = longHistory()
  const late = deferred<Response>()
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = new URL(String(input), 'http://test')
    const memberB = new Headers(init?.headers).get('Authorization') === 'Bearer token-B'
    if (path.pathname === '/api/conversations')
      return json({
        items: [
          { id: memberB ? '601' : '501', created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' },
        ],
        next_cursor: null,
      })
    if (path.pathname === '/api/conversations/501/messages')
      return json(historyPage(data.history, path.searchParams.get('after_id')))
    if (path.pathname === '/api/conversations/601/messages')
      return json({ items: [makeMessage({ id: '1', role: 'user', content: '다른 회원 질문' })], next_cursor: null })
    if (path.pathname === '/api/chat/jobs') {
      if (memberB) return json(jobsPage([]))
      return path.searchParams.has('before_id') ? late.promise : json(jobsPage(data.newest, '7002'))
    }
    throw new Error(`Unexpected request ${String(input)}`)
  })
  const ui = renderWorkspace()
  await userEvent.click(await screen.findByRole('button', { name: /대화 ·/ }))
  await waitFor(() =>
    expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).includes('before_id=7002'))).toBe(true),
  )
  auth.value = { accessToken: 'token-B', user: { id: 'B' } }
  ui.rerender()
  await userEvent.click(await screen.findByRole('button', { name: /대화 ·/ }))
  await screen.findByText('다른 회원 질문')
  await act(async () => {
    late.resolve(json(jobsPage([data.target.job])))
    await late.promise
  })
  expect(screen.queryByText('이전 질문')).toBeNull()
  expect(screen.queryByRole('button', { name: '재시도' })).toBeNull()
  expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull()
})
