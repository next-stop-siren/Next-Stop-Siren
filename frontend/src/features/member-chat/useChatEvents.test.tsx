// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { ChatStreamHttpError, type OpenChatEvents } from './chatEventStream'
import { selectAnswerView } from './chatEventsReducer'
import { makeEnvelope, makeLimits } from './chatQueueBuilders'
import { createMockChatServer, MOCK_ANSWER_TEXT } from './mockChatServer'
import { useChatEvents, type UseChatEventsOptions } from './useChatEvents'

afterEach(() => {
  cleanup()
})

function setup(overrides: Partial<UseChatEventsOptions> = {}) {
  const server = createMockChatServer()
  const open = vi.fn<OpenChatEvents>((options) => server.open(options))
  const initialProps: UseChatEventsOptions = {
    userId: 'u1',
    getAccessToken: () => 'token-1',
    open,
    reconnectDelayMs: () => 0,
    ...overrides,
  }
  const hook = renderHook((props: UseChatEventsOptions) => useChatEvents(props), { initialProps })

  return { server, open, ...hook }
}

function startRunning(server: ReturnType<typeof createMockChatServer>, content = '질문') {
  const accepted = server.accept('501', content)
  server.start(accepted.job.id)
  return accepted.job.id
}

test('구독을 열면 snapshot으로 연결 상태가 open이 된다', async () => {
  const { result } = setup()

  await waitFor(() => expect(result.current.connection).toBe('open'))
  expect(result.current.serverTime).not.toBeNull()
})

test('delta는 임시 출력으로 보이다가, 저장 완료 이벤트가 오면 확정 답변으로 바뀐다', async () => {
  const { server, result } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(server)
  })

  act(() => {
    server.emitDelta(jobId, '도로교통법 ')
    server.emitDelta(jobId, '제160조')
  })
  expect(selectAnswerView(result.current, jobId)).toEqual({ kind: 'draft', text: '도로교통법 제160조' })

  act(() => {
    server.complete(jobId)
  })

  expect(selectAnswerView(result.current, jobId)).toEqual({ kind: 'confirmed', content: MOCK_ANSWER_TEXT })
  expect(result.current.drafts).toEqual({})
})

test('실패하면 임시 출력을 지우고 실패로만 표시한다', async () => {
  const { server, result } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(server)
    server.emitDelta(jobId, '반쪽 답변')
  })
  expect(selectAnswerView(result.current, jobId).kind).toBe('draft')

  act(() => {
    server.fail(jobId)
  })

  expect(selectAnswerView(result.current, jobId)).toMatchObject({ kind: 'failed', errorCode: 'provider_failed' })
  expect(result.current.drafts).toEqual({})
})

test('연결이 끊겨도 서버 작업은 유지되고, 다시 연결하면 끊긴 사이 저장된 결과로 복구한다', async () => {
  const { server, open, result } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(server)
    server.emitDelta(jobId, '앞부분 ')
  })
  const firstSince = result.current.serverTime

  // 탭이 닫히거나 네트워크가 끊겨 구독만 사라진다.
  await act(async () => {
    server.disconnectAll()
  })

  // 구독이 없는 동안에도 서버는 답변을 끝내 저장한다.
  server.emitDelta(jobId, '아무도 못 받은 조각')
  server.complete(jobId)

  await waitFor(() => expect(selectAnswerView(result.current, jobId).kind).toBe('confirmed'))

  expect(selectAnswerView(result.current, jobId)).toEqual({ kind: 'confirmed', content: MOCK_ANSWER_TEXT })
  expect(result.current.drafts).toEqual({})
  // 같은 키로 조회·재구독만 했고, 새 질문은 접수하지 않았다.
  expect(server.listJobs()).toHaveLength(1)
  expect(open).toHaveBeenCalledTimes(2)
  expect(open.mock.calls[0]?.[0].since).toBeNull()
  expect(open.mock.calls[1]?.[0].since).toBe(firstSince)
})

test('재연결 뒤에도 실행 중인 작업은 생성 중으로만 표시하고, 놓친 조각을 임시 출력으로 보이지 않는다', async () => {
  const { server, result } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(server)
    server.emitDelta(jobId, '앞부분 ')
  })

  await act(async () => {
    server.disconnectAll()
  })
  await waitFor(() => expect(server.subscriberCount()).toBe(1))
  await waitFor(() => expect(result.current.connection).toBe('open'))

  act(() => {
    server.emitDelta(jobId, '재연결 뒤 조각')
  })

  expect(selectAnswerView(result.current, jobId)).toEqual({ kind: 'generating' })

  act(() => {
    server.complete(jobId)
  })
  expect(selectAnswerView(result.current, jobId).kind).toBe('confirmed')
})

test('연결이 거절되면 늘어나는 간격으로 다시 시도한다', async () => {
  const server = createMockChatServer()
  const delays: number[] = []
  let failures = 2

  const open: OpenChatEvents = (options) => {
    if (failures > 0) {
      failures -= 1
      return Promise.reject(new ChatStreamHttpError(500))
    }
    return server.open(options)
  }

  const { result } = renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open,
      reconnectDelayMs: (attempt) => {
        delays.push(attempt)
        return 0
      },
    }),
  )

  await waitFor(() => expect(result.current.connection).toBe('open'))
  expect(delays).toEqual([0, 1])
})

test('재연결할 때는 그 시점의 최신 access 토큰을 쓴다', async () => {
  let token = 'token-1'
  const { server, open, result } = setup({ getAccessToken: () => token })
  await waitFor(() => expect(result.current.connection).toBe('open'))

  token = 'token-2'
  await act(async () => {
    server.disconnectAll()
  })
  await waitFor(() => expect(open).toHaveBeenCalledTimes(2))

  expect(open.mock.calls.map(([options]) => options.accessToken)).toEqual(['token-1', 'token-2'])
})

test('로그아웃하면 구독을 끊고 대화·임시 출력·작업 상태를 모두 지운다', async () => {
  const { server, result, rerender } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(server)
    server.emitDelta(jobId, '비공개 임시 출력')
  })
  expect(Object.keys(result.current.jobs)).toEqual([jobId])

  rerender({ userId: null, getAccessToken: () => null })

  expect(server.subscriberCount()).toBe(0)
  expect(result.current.jobs).toEqual({})
  expect(result.current.drafts).toEqual({})
  expect(result.current.limits).toBeNull()
  expect(result.current.serverTime).toBeNull()

  // 서버 작업은 취소되지 않는다.
  expect(server.getEnvelope(jobId)?.job.state).toBe('running')
})

test('다른 회원으로 바뀌면 이전 회원의 상태를 버리고 새 구독을 연다', async () => {
  const { server, open, result, rerender } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))

  act(() => {
    startRunning(server)
  })
  expect(Object.keys(result.current.jobs)).toHaveLength(1)

  rerender({ userId: 'u2', getAccessToken: () => 'token-2', open, reconnectDelayMs: () => 0 })

  // 이전 회원의 since를 새 회원에게 넘기지 않는다.
  await waitFor(() => expect(open).toHaveBeenCalledTimes(2))
  expect(open.mock.calls[1]?.[0].since).toBeNull()
  expect(open.mock.calls[1]?.[0].accessToken).toBe('token-2')
})

test('화면이 사라지면 구독을 끊는다', async () => {
  const { server, result, unmount } = setup()
  await waitFor(() => expect(result.current.connection).toBe('open'))
  expect(server.subscriberCount()).toBe(1)

  unmount()

  expect(server.subscriberCount()).toBe(0)
})

test('재연결 snapshot에 없는 진행 중 작업은 작업 조회로 최종 결과를 받아 반영한다', async () => {
  const running = makeEnvelope({
    job: { id: '7001', state: 'running', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'pending' },
  })
  const completed = makeEnvelope({
    job: { id: '7001', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'completed', content: '조회로 받은 최종 답변' },
  })
  const resolveJob = vi.fn(async (_accessToken: string, _jobId: string) => completed)

  // 첫 연결은 실행 중인 작업을 알려 주고 바로 끊긴다. 재연결 snapshot에서는 그 작업이 빠져 있다.
  let call = 0
  const open: OpenChatEvents = async ({ onEvent, signal }) => {
    call += 1
    onEvent({
      type: 'snapshot',
      data: {
        items: call === 1 ? [running] : [],
        limits: makeLimits(),
        server_time: `2026-10-20T03:00:0${call}Z`,
      },
    })

    if (call === 1) return

    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    })
  }

  const { result } = renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open,
      resolveJob,
      reconnectDelayMs: () => 0,
    }),
  )

  await waitFor(() =>
    expect(selectAnswerView(result.current, '7001')).toEqual({ kind: 'confirmed', content: '조회로 받은 최종 답변' }),
  )
  expect(resolveJob).toHaveBeenCalledWith('token-1', '7001')
  expect(result.current.unresolved).toEqual([])
})
