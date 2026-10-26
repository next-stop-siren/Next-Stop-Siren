// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { ChatStreamHttpError, type OpenChatEvents } from './chatEventStream'
import { selectAnswerView } from './chatEventsReducer'
import { makeEnvelope, makeLimits } from './chatQueueBuilders'
import { createMockChatServer, MOCK_ANSWER_TEXT } from './mockChatServer'
import { MAX_RESOLVE_ATTEMPTS, useChatEvents, type UseChatEventsOptions } from './useChatEvents'

afterEach(() => {
  cleanup()
})

const SERVER_TIME = '2026-10-20T03:00:00Z'

function setup(overrides: Partial<UseChatEventsOptions> & { server?: ReturnType<typeof createMockChatServer> } = {}) {
  const { server = createMockChatServer(), ...hookOverrides } = overrides
  const open = vi.fn<OpenChatEvents>((options) => server.open(options))
  const initialProps: UseChatEventsOptions = {
    userId: 'u1',
    getAccessToken: () => 'token-1',
    open,
    reconnectDelayMs: () => 0,
    ...hookOverrides,
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

// 서버가 snapshot만 보내고 바로 연결을 닫는 상황. 연결이 이어진 시간이 짧다.
function snapshotThenClose(counter: { calls: number }): OpenChatEvents {
  return async ({ onEvent }) => {
    counter.calls += 1
    onEvent({
      type: 'snapshot',
      data: { items: [], limits: makeLimits(), server_time: `2026-10-20T03:00:0${counter.calls % 10}Z` },
    })
  }
}

test('snapshot만 받고 바로 끊기는 연결이 이어지면 재연결 간격이 계속 늘어난다', async () => {
  const counter = { calls: 0 }
  const attempts: number[] = []

  renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open: snapshotThenClose(counter),
      reconnectDelayMs: (attempt) => {
        attempts.push(attempt)
        return 0
      },
    }),
  )

  await waitFor(() => expect(attempts.length).toBeGreaterThanOrEqual(4))
  expect(attempts.slice(0, 4)).toEqual([0, 1, 2, 3])
})

test('충분히 오래 이어진 연결이 끊긴 뒤에는 재연결 간격을 처음부터 다시 센다', async () => {
  const counter = { calls: 0 }
  const attempts: number[] = []

  renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open: snapshotThenClose(counter),
      // 0이면 이어진 시간과 상관없이 안정된 연결로 본다.
      stableConnectionMs: 0,
      reconnectDelayMs: (attempt) => {
        attempts.push(attempt)
        return 0
      },
    }),
  )

  await waitFor(() => expect(attempts.length).toBeGreaterThanOrEqual(3))
  expect(attempts.slice(0, 3)).toEqual([0, 0, 0])
})

function runningThenGone(): { open: OpenChatEvents; running: ReturnType<typeof makeEnvelope> } {
  const running = makeEnvelope({
    job: { id: '7001', state: 'running', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'pending' },
  })
  let call = 0

  // 첫 연결은 실행 중인 작업을 알려 주고 닫힌다. 재연결 snapshot에는 그 작업이 없고 이후로는 안정적으로 열려 있다.
  const open: OpenChatEvents = async ({ onEvent, signal }) => {
    call += 1
    onEvent({
      type: 'snapshot',
      data: { items: call === 1 ? [running] : [], limits: makeLimits(), server_time: `2026-10-20T03:00:0${call}Z` },
    })

    if (call === 1) return

    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    })
  }

  return { open, running }
}

test('작업 조회가 실패해도 잠시 뒤 다시 조회해 최종 결과를 받는다', async () => {
  const { open } = runningThenGone()
  const completed = makeEnvelope({
    job: { id: '7001', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'completed', content: '두 번째 조회로 받은 답변' },
  })
  const resolveJob = vi
    .fn<NonNullable<UseChatEventsOptions['resolveJob']>>()
    .mockRejectedValueOnce(new Error('네트워크 오류'))
    .mockResolvedValue(completed)

  const { result } = renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open,
      resolveJob,
      reconnectDelayMs: () => 0,
      resolveRetryMs: 10,
    }),
  )

  await waitFor(() =>
    expect(selectAnswerView(result.current, '7001')).toEqual({
      kind: 'confirmed',
      content: '두 번째 조회로 받은 답변',
    }),
  )
  expect(resolveJob).toHaveBeenCalledTimes(2)
})

test('조회가 계속 실패하면 정해진 횟수까지만 시도하고 멈춘다', async () => {
  const { open } = runningThenGone()
  const resolveJob = vi
    .fn<NonNullable<UseChatEventsOptions['resolveJob']>>()
    .mockRejectedValue(new Error('네트워크 오류'))

  const { result } = renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open,
      resolveJob,
      reconnectDelayMs: () => 0,
      resolveRetryMs: 5,
    }),
  )

  await waitFor(() => expect(resolveJob).toHaveBeenCalledTimes(MAX_RESOLVE_ATTEMPTS))
  // 멈췄는지 확인하려면 더 시도할 시간을 줘야 한다.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 100))
  })

  expect(resolveJob).toHaveBeenCalledTimes(MAX_RESOLVE_ATTEMPTS)
  expect(result.current.unresolved).toEqual(['7001'])
})

test('완료라고 왔는데 답변 내용이 비어 있으면 저장된 답변을 조회해 확정 답변으로 바꾼다', async () => {
  const emptyAnswer = makeEnvelope({
    job: { id: '7001', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'completed', content: null },
  })
  const full = makeEnvelope({
    job: { id: '7001', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'completed', content: '조회로 받은 저장된 답변' },
  })
  const resolveJob = vi.fn(async (_accessToken: string, _jobId: string) => full)

  const open: OpenChatEvents = async ({ onEvent, signal }) => {
    onEvent({
      type: 'snapshot',
      data: { items: [], limits: makeLimits(), server_time: '2026-10-20T03:00:01Z' },
    })
    onEvent({ type: 'job', data: emptyAnswer })

    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    })
  }

  const { result } = renderHook(() =>
    useChatEvents({ userId: 'u1', getAccessToken: () => 'token-1', open, resolveJob, resolveRetryMs: 10 }),
  )

  await waitFor(() =>
    expect(selectAnswerView(result.current, '7001')).toEqual({ kind: 'confirmed', content: '조회로 받은 저장된 답변' }),
  )
  expect(resolveJob).toHaveBeenCalledTimes(1)
})

// --- 작업 다시 읽기 (#21) ---

/** 실패한 질문 하나를 만들고 구독을 끊어 둔다. 이후 서버에서 바뀌는 일은 화면이 이벤트로 받지 못한다. */
async function setupDisconnectedWithFailure(overrides: Partial<UseChatEventsOptions> = {}) {
  const ctx = setup({
    reconnectDelayMs: () => 60_000,
    resolveJob: async (_token, id) => ctx.server.getEnvelope(id),
    ...overrides,
  })
  await waitFor(() => expect(ctx.result.current.connection).toBe('open'))

  let jobId = ''
  act(() => {
    jobId = startRunning(ctx.server)
    ctx.server.fail(jobId)
  })

  act(() => {
    ctx.server.disconnectAll()
  })
  await waitFor(() => expect(ctx.result.current.connection).toBe('reconnecting'))

  return { ...ctx, jobId }
}

test('refreshJob은 구독 이벤트를 놓쳐 뒤처진 작업을 지금 서버 상태로 맞춘다', async () => {
  const { server, result, jobId } = await setupDisconnectedWithFailure()
  expect(result.current.jobs[jobId]?.job.allowed_actions).toEqual(['retry', 'skip'])

  act(() => {
    server.skip(jobId)
  })
  // 구독이 끊겨 있어 화면은 아직 모른다.
  expect(result.current.jobs[jobId]?.job.allowed_actions).toEqual(['retry', 'skip'])

  await act(async () => {
    await result.current.refreshJob(jobId)
  })

  expect(result.current.jobs[jobId]?.job.allowed_actions).toEqual(['retry'])
})

test('refreshJob이 가져오지 못하거나 작업이 없으면 상태를 바꾸지 않고 던지지도 않는다', async () => {
  const resolveJob = vi
    .fn<NonNullable<UseChatEventsOptions['resolveJob']>>()
    .mockRejectedValueOnce(new Error('network'))
    .mockResolvedValueOnce(null)
  const { result, jobId } = await setupDisconnectedWithFailure({ resolveJob })
  const before = result.current.jobs[jobId]

  await act(async () => {
    await result.current.refreshJob(jobId)
    await result.current.refreshJob(jobId)
  })

  expect(resolveJob).toHaveBeenCalledTimes(2)
  expect(result.current.jobs[jobId]).toBe(before)
})

test('refreshJob은 사용자가 바뀐 뒤 늦게 온 이전 사용자의 응답을 버린다', async () => {
  let release: (envelope: ReturnType<typeof makeEnvelope> | null) => void = () => undefined
  const resolveJob = vi.fn<NonNullable<UseChatEventsOptions['resolveJob']>>(
    () =>
      new Promise((resolve) => {
        release = resolve
      }),
  )
  const { result, rerender } = setup({ resolveJob })
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let pending: Promise<void> = Promise.resolve()
  act(() => {
    pending = result.current.refreshJob('7001')
  })

  rerender({ userId: 'u2', getAccessToken: () => 'token-2', reconnectDelayMs: () => 0 })

  await act(async () => {
    release(makeEnvelope({ job: { id: '7001', state: 'queued' } }))
    await pending
  })

  expect(result.current.jobs['7001']).toBeUndefined()
})

test('refreshJob은 토큰이 없으면 조회하지 않는다', async () => {
  const resolveJob = vi.fn<NonNullable<UseChatEventsOptions['resolveJob']>>(async () => null)
  const { result } = setup({ resolveJob, getAccessToken: () => null })

  await act(async () => {
    await result.current.refreshJob('7001')
  })

  expect(resolveJob).not.toHaveBeenCalled()
})

test('refreshJob: 조회하는 동안 같은 작업의 구독 이벤트가 먼저 오면, 늦게 온 조회 결과로 버튼을 되돌리지 않는다', async () => {
  const server = createMockChatServer()
  let staleEnvelope: ReturnType<typeof server.getEnvelope> = null
  let release: () => void = () => undefined
  // 조회는 재시도 전의 상태(재시도·건너뛰기 버튼)를 담아 늦게 돌아온다.
  const resolveJob = vi.fn<NonNullable<UseChatEventsOptions['resolveJob']>>(
    () =>
      new Promise((resolve) => {
        release = () => resolve(staleEnvelope)
      }),
  )
  const { result } = setup({ server, resolveJob })
  await waitFor(() => expect(result.current.connection).toBe('open'))

  let failed = ''
  act(() => {
    failed = startRunning(server)
    server.fail(failed)
    server.advance(10)
  })
  staleEnvelope = server.getEnvelope(failed)
  expect(result.current.jobs[failed]?.job.allowed_actions).toEqual(['retry', 'skip'])

  let pending: Promise<void> = Promise.resolve()
  act(() => {
    pending = result.current.refreshJob(failed)
  })

  // 조회 응답이 오기 전에 구독으로 재시도 이벤트가 먼저 도착한다. 원래 실패 작업의 버튼은 사라진다.
  act(() => {
    server.retry(failed, 'key-1')
  })
  expect(result.current.jobs[failed]?.job.allowed_actions).toEqual([])

  await act(async () => {
    release()
    await pending
  })

  expect(result.current.jobs[failed]?.job.allowed_actions).toEqual([])
})

test('refreshJob: 조회하는 동안 그 작업의 이벤트가 없었다면 조회 결과를 반영한다', async () => {
  const { server, result, jobId } = await setupDisconnectedWithFailure()

  act(() => {
    server.skip(jobId)
  })
  await act(async () => {
    await result.current.refreshJob(jobId)
  })

  expect(result.current.jobs[jobId]?.job.allowed_actions).toEqual(['retry'])
})

test('snapshot에서 빠진 작업을 조회하는 동안 그 작업의 새 이벤트가 오면, 늦게 온 조회 결과로 버튼을 되돌리지 않는다', async () => {
  const running = makeEnvelope({
    job: { id: '7001', state: 'running', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'pending' },
  })
  // 조회 결과는 재시도 전 상태(재시도·건너뛰기 버튼)이고, 그 사이 구독으로 더 최신인 같은 상태의 이벤트가 먼저 도착한다.
  const failedStale = makeEnvelope({
    job: { id: '7001', state: 'failed', position: null, allowed_actions: ['retry', 'skip'] },
    answer: { id: '9002', status: 'failed', error_code: 'provider_failed' },
  })
  const failedNewer = makeEnvelope({
    job: { id: '7001', state: 'failed', position: null, allowed_actions: [] },
    answer: { id: '9002', status: 'failed', error_code: 'provider_failed' },
  })
  let release: () => void = () => undefined
  const resolveJob = vi.fn<NonNullable<UseChatEventsOptions['resolveJob']>>(
    () =>
      new Promise((resolve) => {
        release = () => resolve(failedStale)
      }),
  )

  let call = 0
  let pushEvent: Parameters<OpenChatEvents>[0]['onEvent'] = () => undefined
  const open: OpenChatEvents = async ({ onEvent, signal }) => {
    call += 1
    pushEvent = onEvent
    onEvent({
      type: 'snapshot',
      data: { items: call === 1 ? [running] : [], limits: makeLimits(), server_time: `2026-10-20T03:00:0${call}Z` },
    })

    if (call === 1) return

    await new Promise<void>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
    })
  }

  const { result } = renderHook(() =>
    useChatEvents({ userId: 'u1', getAccessToken: () => 'token-1', open, resolveJob, reconnectDelayMs: () => 0 }),
  )

  await waitFor(() => expect(resolveJob).toHaveBeenCalledTimes(1))

  act(() => {
    pushEvent({ type: 'job', data: failedNewer })
  })
  expect(result.current.jobs['7001']?.job.allowed_actions).toEqual([])

  await act(async () => {
    release()
    await Promise.resolve()
  })

  expect(result.current.jobs['7001']?.job.allowed_actions).toEqual([])
})

test('화면을 떠난 뒤 늦게 끝난 작업 조회는 다시 조회할 타이머를 만들지 않는다', async () => {
  const RETRY_MS = 4321
  let fail: () => void = () => {}
  const resolveJob = vi.fn(
    () =>
      new Promise<never>((_resolve, reject) => {
        fail = () => reject(new Error('조회 실패'))
      }),
  )
  const running = makeEnvelope({ job: { state: 'running', position: null, allowed_actions: [] } })
  let call = 0
  const open: OpenChatEvents = async ({ onEvent, signal }) => {
    call += 1
    // 첫 연결의 진행 중 작업이 두 번째 snapshot에서 빠져 최종 결과를 조회하게 만든다.
    onEvent({
      type: 'snapshot',
      data: { items: call === 1 ? [running] : [], limits: makeLimits(), server_time: SERVER_TIME },
    })
    if (call === 1) return
    await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  }

  const { unmount } = renderHook(() =>
    useChatEvents({
      userId: 'u1',
      getAccessToken: () => 'token-1',
      open,
      resolveJob,
      reconnectDelayMs: () => 0,
      resolveRetryMs: RETRY_MS,
    }),
  )
  await waitFor(() => expect(resolveJob).toHaveBeenCalledTimes(1))

  unmount()
  const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout')
  await act(async () => {
    fail()
    await Promise.resolve()
    await Promise.resolve()
  })

  expect(setTimeoutSpy.mock.calls.filter(([, delay]) => delay === RETRY_MS)).toEqual([])
  setTimeoutSpy.mockRestore()
})

test('applyResponse: 요청을 보낸 뒤 같은 작업·한도의 구독 이벤트가 먼저 왔다면 늦은 응답 값을 버린다', async () => {
  let pushEvent: Parameters<OpenChatEvents>[0]['onEvent'] = () => undefined
  const open: OpenChatEvents = ({ onEvent, signal }) => {
    pushEvent = onEvent
    onEvent({ type: 'snapshot', data: { items: [], limits: makeLimits(), server_time: SERVER_TIME } })
    return new Promise((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  }
  const { result } = renderHook(() =>
    useChatEvents({ userId: 'u1', getAccessToken: () => 'token-1', open, reconnectDelayMs: () => 0 }),
  )
  await waitFor(() => expect(result.current.connection).toBe('open'))

  const mark = result.current.mark()
  const running = makeEnvelope({ job: { state: 'running', position: null, allowed_actions: [] } })
  const newerLimits = makeLimits({ next_attempt_at: '2026-10-20T03:00:10Z' })
  act(() => {
    pushEvent({ type: 'job', data: running })
    pushEvent({ type: 'limits', data: newerLimits })
  })

  // 접수 응답은 요청 시점의 대기 상태와 그때의 한도를 담고 있다.
  act(() => {
    result.current.applyResponse(mark, { envelopes: [makeEnvelope()], limits: makeLimits() })
  })

  expect(result.current.jobs['7001']?.job.state).toBe('running')
  expect(result.current.limits).toEqual(newerLimits)

  // 그 뒤에 보낸 요청의 응답은 반영한다.
  const later = result.current.mark()
  const other = makeEnvelope({ job: { id: '7002', question_message_id: '9003' } })
  act(() => {
    result.current.applyResponse(later, { envelopes: [other], limits: makeLimits() })
  })
  expect(result.current.jobs['7002']?.job.state).toBe('queued')
  expect(result.current.limits?.next_attempt_at).toBeNull()
})

test('applyResponse: 회원이 바뀐 뒤 도착한 이전 회원의 응답은 버린다', async () => {
  const open: OpenChatEvents = ({ onEvent, signal }) => {
    onEvent({ type: 'snapshot', data: { items: [], limits: makeLimits(), server_time: SERVER_TIME } })
    return new Promise((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  }
  const { result, rerender } = renderHook(
    ({ userId }: { userId: string }) =>
      useChatEvents({ userId, getAccessToken: () => 'token', open, reconnectDelayMs: () => 0 }),
    { initialProps: { userId: 'u1' } },
  )
  await waitFor(() => expect(result.current.connection).toBe('open'))
  const mark = result.current.mark()

  rerender({ userId: 'u2' })
  act(() => {
    result.current.applyResponse(mark, { envelopes: [makeEnvelope()], limits: makeLimits() })
  })

  expect(result.current.jobs).toEqual({})
})
