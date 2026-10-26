// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { conversationLabel } from '../layout/Sidebar'
import type { OpenChatEvents } from './chatEventStream'
import { makeEnvelope, makeJob, makeLimits, makeMessage } from './chatQueueBuilders'
import type { ChatEvent, ChatJob, ChatJobEnvelope, ChatLimits } from './chatQueueTypes'
import type { ApiMessage } from './memberChatApiTypes'
import MemberWorkspace from './MemberWorkspace'

// 회원 화면은 인증 상태만 읽는다. 로그인 절차는 AuthContext 테스트가 다루므로 값을 직접 주입한다.
const auth = vi.hoisted(() => ({
  value: { accessToken: 'token-1', user: { id: 'u1' } } as {
    accessToken: string | null
    user: { id: string } | null
  },
}))

vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth.value }))

type Handler = (url: string, init?: RequestInit) => Response | undefined | Promise<Response | undefined>

beforeEach(() => {
  auth.value = { accessToken: 'token-1', user: { id: 'u1' } }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const SERVER_TIME = '2026-10-20T03:00:00Z'

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

// 늦게 도착한 응답이 처리될 시간을 주고 화면을 안정시킨다. "아무 일도 일어나지 않아야 한다"는 검사는
// 응답을 처리할 기회를 준 뒤에 해야 의미가 있다. 기다리기만 하는 waitFor는 이미 참이면 바로 끝나 버린다.
async function settle(ms = 30) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms))
  })
}

/**
 * fetch를 가로챈다. 대화의 작업 목록은 따로 정하지 않으면 빈 목록이다.
 * 예상하지 못한 호출은 실패로 기록해 테스트가 놓치지 않게 한다.
 */
function mockApi(handler: Handler) {
  const unexpected: string[] = []
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    const response = await handler(url, init)
    if (response) return response
    if (url.startsWith('/api/chat/jobs?conversation_id=')) return json(200, jobPage([]))
    unexpected.push(`${init?.method ?? 'GET'} ${url}`)
    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${init?.method ?? 'GET'} ${url}`)
  })
  return Object.assign(spy, { unexpected })
}

type Connection = {
  since: string | null | undefined
  accessToken: string
  signal: AbortSignal
  emit: (event: ChatEvent) => void
  close: () => void
}

/** 회원 단위 구독을 대신한다. 연결마다 이벤트를 직접 보내고 끊을 수 있다. */
function fakeEvents() {
  const connections: Connection[] = []
  const open: OpenChatEvents = ({ accessToken, since, signal, onEvent }) =>
    new Promise<void>((resolve) => {
      connections.push({
        since,
        accessToken,
        signal,
        emit: (event) => act(() => onEvent(event)),
        close: () => act(() => resolve()),
      })
      signal.addEventListener('abort', () => resolve(), { once: true })
    })

  return {
    open,
    connections,
    get last(): Connection {
      const connection = connections[connections.length - 1]
      if (!connection) throw new Error('구독이 아직 열리지 않았습니다.')
      return connection
    },
  }
}

function renderChat(options: { requestTimeoutMs?: number } = {}) {
  const events = fakeEvents()
  const ui = () => (
    <div className="app-layout">
      {auth.value.user && (
        <MemberWorkspace
          key={auth.value.user.id}
          onLogoutClick={() => {}}
          options={{
            events: { open: events.open, reconnectDelayMs: () => 0, resolveRetryMs: 10 },
            requestTimeoutMs: options.requestTimeoutMs,
          }}
        />
      )}
    </div>
  )
  const result = render(ui())

  return { ...result, events, rerender: () => result.rerender(ui()) }
}

function userMessage(id: string, content: string, requestKey = `key-${id}`): ApiMessage {
  return makeMessage({ id, role: 'user', status: 'completed', content, request_key: requestKey })
}

function assistantMessage(
  id: string,
  replyTo: string,
  status: ApiMessage['status'],
  content: string | null,
  errorCode: string | null = null,
  attemptNo = 1,
): ApiMessage {
  return makeMessage({
    id,
    role: 'assistant',
    status,
    content,
    reply_to_message_id: replyTo,
    attempt_no: attemptNo,
    error_code: errorCode,
  })
}

/** 대화마다 만든 시각을 달리해 사이드바 이름이 겹치지 않게 한다. */
function conversation(id: string) {
  const created = new Date(Date.UTC(2026, 9, 21, 0, Number(id) % 60)).toISOString()
  return { id, created_at: created, updated_at: created }
}

function page(items: unknown[], nextCursor: string | null = null) {
  return { items, next_cursor: nextCursor }
}

function jobPage(items: ChatJob[], limits: ChatLimits = makeLimits()) {
  return { items, next_cursor: null, limits, server_time: SERVER_TIME }
}

function snapshot(items: ChatJobEnvelope[] = [], limits = makeLimits(), serverTime = SERVER_TIME): ChatEvent {
  return { type: 'snapshot', data: { items, limits, server_time: serverTime } }
}

function admission(envelope: ChatJobEnvelope, limits = makeLimits()) {
  return { question: envelope.question, answer: envelope.answer, job: envelope.job, limits }
}

function conversationButton(id: string) {
  return screen.getByRole('button', { name: conversationLabel(conversation(id)) })
}

async function findConversationButton(id: string) {
  return screen.findByRole('button', { name: conversationLabel(conversation(id)) })
}

function postCalls(fetchSpy: ReturnType<typeof mockApi>) {
  return fetchSpy.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([input, init]) => ({
      url: String(input),
      body: JSON.parse(String(init?.body)) as { content: string; request_key: string; retry_key: string },
    }))
}

function questionPosts(fetchSpy: ReturnType<typeof mockApi>) {
  return postCalls(fetchSpy).filter((call) => /\/api\/conversations\/\d+\/messages$/.test(call.url))
}

function authHeader(init?: RequestInit): string {
  return new Headers(init?.headers).get('Authorization') ?? ''
}

/** 질문을 보내면 접수만 하고 대기 중인 작업을 돌려주는 기본 응답 */
function acceptQuestion(conversationId: string, init?: RequestInit, jobId = '7001', questionId = '9001') {
  const body = JSON.parse(String(init?.body)) as { content: string; request_key: string }
  return json(
    202,
    admission(
      makeEnvelope({
        job: { id: jobId, conversation_id: conversationId, question_message_id: questionId },
        question: { content: body.content, request_key: body.request_key },
      }),
    ),
  )
}

function textarea() {
  return screen.getByLabelText('질문 입력') as HTMLTextAreaElement
}

function sendButton() {
  return screen.getByRole('button', { name: /질문 보내기|다시 확인하기|전송 중/ }) as HTMLButtonElement
}

// --- 대화·이력 읽기 ---

test('첫 페이지 이후에도 next_cursor를 따라 최신 메시지까지 읽는다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      return json(200, page([userMessage('1', 'Q-FIRST'), assistantMessage('2', '1', 'completed', 'A-FIRST')], '2'))
    }
    if (url === '/api/conversations/501/messages?limit=100&after_id=2') {
      return json(200, page([userMessage('3', 'Q-LAST'), assistantMessage('4', '3', 'completed', 'A-LAST')]))
    }
    return undefined
  })

  renderChat()

  expect(await screen.findByText('A-LAST')).toBeTruthy()
  expect(screen.getByText('A-FIRST')).toBeTruthy()
})

test('대화 목록도 next_cursor를 따라 모두 읽고, 사이드바에 같은 대화를 현재 대화로 표시한다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('503')], '503'))
    if (url === '/api/conversations?limit=20&before_id=503') return json(200, page([conversation('502')]))
    if (url === '/api/conversations/503/messages?limit=100') return json(200, page([userMessage('1', 'Q-503')]))
    return undefined
  })

  renderChat()

  expect(await findConversationButton('502')).toBeTruthy()
  await screen.findByText('Q-503')
  expect(conversationButton('503').getAttribute('aria-current')).toBe('page')
  expect(conversationButton('502').getAttribute('aria-current')).toBeNull()
})

test('사이드바에서 대화를 고르면 본문이 그 대화를 보여 주고, 새 대화는 빈 화면에서 시작한다', async () => {
  const user = userEvent.setup()

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url === '/api/conversations/502/messages?limit=100') return json(200, page([userMessage('1', 'Q-502')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('2', 'Q-501')]))
    return undefined
  })

  renderChat()
  await screen.findByText('Q-502')

  await user.click(conversationButton('501'))
  expect(await screen.findByText('Q-501')).toBeTruthy()
  expect(screen.queryByText('Q-502')).toBeNull()
  expect(conversationButton('501').getAttribute('aria-current')).toBe('page')

  await user.click(screen.getByRole('button', { name: '＋ 새 대화' }))
  expect(screen.queryByText('Q-501')).toBeNull()
  expect(screen.getByText('교통법에 대해 질문해 보세요')).toBeTruthy()
  expect(conversationButton('501').getAttribute('aria-current')).toBeNull()
})

test('access 토큰이 갱신돼도 선택한 대화가 유지되고 목록을 다시 읽지 않는다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url === '/api/conversations/502/messages?limit=100') return json(200, page([userMessage('1', 'Q-502')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('2', 'Q-501')]))
    return undefined
  })

  const { rerender, events } = renderChat()

  await screen.findByText('Q-502')
  await user.click(conversationButton('501'))
  await screen.findByText('Q-501')

  // 토큰만 새 값으로 바뀐다. 사용자는 그대로다.
  auth.value = { accessToken: 'token-2', user: { id: 'u1' } }
  rerender()
  await settle()

  expect(conversationButton('501').getAttribute('aria-current')).toBe('page')
  expect(screen.getByText('Q-501')).toBeTruthy()

  const listCalls = fetchSpy.mock.calls.filter(([input]) => String(input) === '/api/conversations?limit=20')
  expect(listCalls).toHaveLength(1)
  // 토큰이 바뀌어도 구독을 새로 열지 않는다. 다음 재연결 때 새 토큰을 쓴다.
  expect(events.connections).toHaveLength(1)
})

test('토큰 갱신 뒤의 요청과 재연결은 새 토큰을 쓴다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages') return acceptQuestion('501', init)
    return undefined
  })

  const { rerender, events } = renderChat()
  await findConversationButton('501')

  auth.value = { accessToken: 'token-2', user: { id: 'u1' } }
  rerender()

  await user.type(textarea(), '질문')
  await user.click(sendButton())

  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(1))

  const post = fetchSpy.mock.calls.find(([, init]) => init?.method === 'POST')
  expect(authHeader(post?.[1])).toBe('Bearer token-2')

  // 서버는 연결을 연 토큰이 만료되면 스트림을 끝낸다. 다시 열 때는 새 토큰을 쓴다.
  events.last.close()
  await waitFor(() => expect(events.connections).toHaveLength(2))
  expect(events.last.accessToken).toBe('token-2')
})

test('작업 없이 이력에만 있는 실패·중단·진행 중 답변은 내용이 있어도 완료 답변으로 보이지 않는다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      return json(
        200,
        page([
          userMessage('1', 'Q-FAILED'),
          assistantMessage('2', '1', 'failed', 'PARTIAL-FAILED', 'provider_failed'),
          userMessage('3', 'Q-INTERRUPTED'),
          assistantMessage('4', '3', 'interrupted', 'PARTIAL-INTERRUPTED', 'outcome_unknown'),
          userMessage('5', 'Q-PENDING'),
          assistantMessage('6', '5', 'pending', 'PARTIAL-PENDING'),
          userMessage('7', 'Q-DONE'),
          assistantMessage('8', '7', 'completed', 'FINAL-ANSWER'),
        ]),
      )
    }
    return undefined
  })

  renderChat()

  expect(await screen.findByText('FINAL-ANSWER')).toBeTruthy()
  expect(screen.queryByText('PARTIAL-FAILED')).toBeNull()
  expect(screen.queryByText('PARTIAL-INTERRUPTED')).toBeNull()
  expect(screen.queryByText('PARTIAL-PENDING')).toBeNull()
  expect(screen.getByText('답변을 만들지 못했습니다. (오류 코드: provider_failed)')).toBeTruthy()
  expect(screen.getByText('답변 생성이 중단되었습니다. (오류 코드: outcome_unknown)')).toBeTruthy()
  expect(screen.getByText('저장된 답변을 확인하는 중입니다.')).toBeTruthy()
})

test('대화를 빠르게 바꿔도 늦게 도착한 이전 대화의 이력이 지금 대화를 덮어쓰지 않는다', async () => {
  const user = userEvent.setup()
  const slow501 = deferred<Response | undefined>()

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url === '/api/conversations/502/messages?limit=100') return json(200, page([userMessage('1', 'Q-502')]))
    if (url === '/api/conversations/501/messages?limit=100') return slow501.promise
    return undefined
  })

  renderChat()
  await screen.findByText('Q-502')

  await user.click(conversationButton('501'))
  await user.click(conversationButton('502'))
  await screen.findByText('Q-502')

  slow501.resolve(json(200, page([userMessage('2', 'Q-501-LATE')])))
  await settle()

  expect(screen.queryByText('Q-501-LATE')).toBeNull()
  expect(screen.getByText('Q-502')).toBeTruthy()
  expect(conversationButton('502').getAttribute('aria-current')).toBe('page')
})

// --- 접수(202)부터 저장된 답변까지 ---

test('202 접수 뒤 대기 → 생성 중 임시 출력 → 저장된 답변으로 이어지고, 실행 중에는 취소 버튼이 없다', async () => {
  const user = userEvent.setup()

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') return acceptQuestion('501', init)
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot())

  await user.type(textarea(), '어린이보호구역 제한속도는?')
  await user.click(sendButton())

  // 응답의 job이 바로 보인다. 구독 이벤트를 기다리지 않는다.
  expect(await screen.findByText('대기 중 · 1번째')).toBeTruthy()
  expect(screen.getByText('어린이보호구역 제한속도는?')).toBeTruthy()
  expect(screen.getByRole('button', { name: '질문 취소' })).toBeTruthy()
  expect(screen.getByText(/만료까지 5분/)).toBeTruthy()
  expect(textarea().value).toBe('')

  const running = makeEnvelope({
    job: {
      id: '7001',
      state: 'running',
      position: null,
      allowed_actions: [],
      started_at: '2026-10-20T03:00:01Z',
    },
    question: { content: '어린이보호구역 제한속도는?' },
    answer: { id: '9002' },
  })
  events.last.emit({ type: 'job', data: running })
  expect(screen.getByText('답변 생성 중입니다.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '질문 취소' })).toBeNull()

  events.last.emit({ type: 'delta', data: { job_id: '7001', text: '도로교통법상 ' } })
  events.last.emit({ type: 'delta', data: { job_id: '7001', text: '시속 30km' } })
  expect(screen.getByText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')).toBeTruthy()
  expect(screen.getByText('도로교통법상 시속 30km')).toBeTruthy()

  events.last.emit({
    type: 'job',
    data: makeEnvelope({
      job: { id: '7001', state: 'completed', position: null, allowed_actions: [] },
      question: { content: '어린이보호구역 제한속도는?' },
      answer: { id: '9002', status: 'completed', content: '저장된 최종 답변: 시속 30km 이내' },
    }),
  })

  expect(screen.getByText('답변 완료 · 저장됨')).toBeTruthy()
  expect(screen.getByText('저장된 최종 답변: 시속 30km 이내')).toBeTruthy()
  expect(screen.queryByText('도로교통법상 시속 30km')).toBeNull()
})

test('구독 이벤트가 접수 응답보다 먼저 와도 늦은 응답이 진행 상태를 대기로 되돌리지 않는다', async () => {
  const user = userEvent.setup()
  const slowPost = deferred<Response | undefined>()
  let postInit: RequestInit | undefined

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      postInit = init
      return slowPost.promise
    }
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))

  await user.type(textarea(), '질문')
  await user.click(sendButton())
  await waitFor(() => expect(postInit).toBeDefined())

  events.last.emit({
    type: 'job',
    data: makeEnvelope({
      job: { id: '7001', state: 'running', position: null, allowed_actions: [] },
      question: { content: '질문' },
      answer: { id: '9002' },
    }),
  })

  slowPost.resolve(acceptQuestion('501', postInit))
  await settle()

  expect(screen.getByText('답변 생성 중입니다.')).toBeTruthy()
  expect(screen.queryByText('대기 중 · 1번째')).toBeNull()
})

test('연결이 끊기면 마지막 server_time을 since로 다시 열고, 끊긴 사이 끝난 답변을 snapshot의 저장 결과로 보여 준다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('9001', '질문')]))
    return undefined
  })

  const { events } = renderChat()
  await screen.findByText('질문')
  await waitFor(() => expect(events.connections).toHaveLength(1))

  const running = makeEnvelope({
    job: { state: 'running', position: null, allowed_actions: [] },
    question: { content: '질문' },
    answer: { id: '9002' },
  })
  events.last.emit(snapshot([running], makeLimits(), '2026-10-20T03:00:10Z'))
  expect(screen.getByText('답변 생성 중입니다.')).toBeTruthy()

  events.last.close()
  await waitFor(() => expect(events.connections).toHaveLength(2))
  expect(events.last.since).toBe('2026-10-20T03:00:10Z')

  events.last.emit(
    snapshot(
      [
        makeEnvelope({
          job: { state: 'completed', position: null, allowed_actions: [] },
          question: { content: '질문' },
          answer: { id: '9002', status: 'completed', content: '끊긴 사이 저장된 답변' },
        }),
      ],
      makeLimits(),
      '2026-10-20T03:00:20Z',
    ),
  )

  expect(screen.getByText('끊긴 사이 저장된 답변')).toBeTruthy()
})

// --- 응답 유실·같은 키 ---

test('전송이 서버 오류로 실패하면 입력이 풀리고, 다시 보낼 때 같은 request_key와 본문을 쓴다', async () => {
  const user = userEvent.setup()
  let posts = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      posts += 1
      if (posts === 1) return json(503, { error: { code: 'temporarily_unavailable', message: '일시적인 오류입니다.' } })
      return acceptQuestion('501', init)
    }
    return undefined
  })

  renderChat()
  await findConversationButton('501')

  await user.type(textarea(), '  같은 질문  ')
  await user.click(sendButton())

  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toContain('일시적인 오류입니다.')
  expect(alert.textContent).toContain('중복으로 접수되지 않습니다')
  expect(textarea().disabled).toBe(false)
  expect(sendButton().textContent).toBe('다시 확인하기')

  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(2))

  const [first, second] = questionPosts(fetchSpy)
  expect(second?.body.request_key).toBe(first?.body.request_key)
  expect(second?.body.content).toBe(first?.body.content)
  // 서버는 질문 원문을 보존하므로 앞뒤 공백도 그대로 보낸다.
  expect(first?.body.content).toBe('  같은 질문  ')
})

test('응답이 오지 않으면 시간 초과로 입력을 풀고, 한도 0·10초 간격이어도 같은 키로 다시 확인할 수 있다', async () => {
  const user = userEvent.setup()
  let posts = 0
  const exhausted = makeLimits({
    daily: { limit: 400, used: 399, reserved: 1, remaining: 0, resets_at: '2026-10-20T15:00:00Z' },
    next_attempt_at: '2026-10-20T03:00:10Z',
  })

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      posts += 1
      // 첫 요청은 서버가 접수했지만 응답이 오지 않는다. 화면이 기다리기를 그만두면 끊긴다.
      if (posts === 1) {
        return new Promise<Response>((_, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        })
      }
      // 같은 키 재조회는 새로 예약하지 않고 이미 접수된 작업과 지금 한도를 돌려준다.
      const body = JSON.parse(String(init.body)) as { content: string; request_key: string }
      return json(
        202,
        admission(makeEnvelope({ question: { content: body.content, request_key: body.request_key } }), exhausted),
      )
    }
    return undefined
  })

  const { events } = renderChat({ requestTimeoutMs: 50 })
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))

  await user.type(textarea(), '응답을 잃을 질문')
  await user.click(sendButton())
  expect(textarea().disabled).toBe(true)

  expect((await screen.findByRole('alert')).textContent).toContain('질문이 접수됐는지 확인하지 못했습니다')
  expect(textarea().disabled).toBe(false)

  // 첫 요청이 접수되며 한도를 다 썼고, 다음 접수까지 10초를 기다려야 한다는 소식이 구독으로 온다.
  events.last.emit(snapshot([], exhausted))

  // 같은 질문을 다시 확인하는 일은 막지 않는다.
  expect(sendButton().disabled).toBe(false)
  expect(screen.getByText('응답을 받지 못한 질문입니다. 다시 보내면 같은 질문으로 확인합니다.')).toBeTruthy()

  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(2))
  const [first, second] = questionPosts(fetchSpy)
  expect(second?.body.request_key).toBe(first?.body.request_key)
  await screen.findByText('대기 중 · 1번째')

  // 새 질문은 한도 때문에 막힌다.
  await user.type(textarea(), '새 질문')
  expect(sendButton().disabled).toBe(true)
  expect(screen.getByText(/질문 한도를 모두 썼습니다/)).toBeTruthy()
})

test('응답을 잃은 질문이 실제로 접수돼 구독으로 나타나면 보관한 키와 입력 내용을 정리한다', async () => {
  const user = userEvent.setup()
  let firstBody: { content: string; request_key: string } | undefined

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      if (!firstBody) {
        firstBody = JSON.parse(String(init.body))
        return json(503, { error: { message: '일시적인 오류입니다.' } })
      }
      return acceptQuestion('501', init, '7002', '9003')
    }
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot())

  await user.type(textarea(), '응답을 잃었지만 접수된 질문')
  await user.click(sendButton())
  await screen.findByRole('alert')
  expect(sendButton().textContent).toBe('다시 확인하기')

  // 서버는 접수했고, 그 작업이 구독으로 온다.
  events.last.emit({
    type: 'job',
    data: makeEnvelope({
      job: { id: '7001', conversation_id: '501', question_message_id: '9001' },
      question: { content: firstBody?.content, request_key: firstBody?.request_key },
    }),
  })

  expect(screen.getByText('응답을 잃었지만 접수된 질문')).toBeTruthy()
  expect(textarea().value).toBe('')

  // 같은 문장을 다시 쓰면 새 질문이라 새 키를 쓴다.
  await user.type(textarea(), '응답을 잃었지만 접수된 질문')
  expect(sendButton().textContent).toBe('질문 보내기')
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(2))
  const [first, second] = questionPosts(fetchSpy)
  expect(second?.body.request_key).not.toBe(first?.body.request_key)
})

test('한도 표시는 새 질문 대기·재시도 대기·실행 중을 따로 보여 준다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(
    snapshot(
      [],
      makeLimits({ queue: { running: 1, questions_waiting: 2, questions_max: 3, retries_waiting: 1, retries_max: 1 } }),
    ),
  )

  expect(screen.getByText(/새 질문 대기 2\/3 · 재시도 대기 1\/1 · 실행 중 1/)).toBeTruthy()
})

test('한도·대기 상태에 따라 입력을 막고, 재시도 대기가 가득 찬 것은 새 질문을 막지 않는다', async () => {
  const user = userEvent.setup()

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  await user.type(textarea(), '질문')

  const queue = makeLimits().queue
  events.last.emit({
    type: 'limits',
    data: makeLimits({ queue: { ...queue, retries_waiting: 1, retries_max: 1 } }),
  })
  expect(sendButton().disabled).toBe(false)

  events.last.emit({
    type: 'limits',
    data: makeLimits({ queue: { ...queue, questions_waiting: 3, questions_max: 3 } }),
  })
  expect(sendButton().disabled).toBe(true)
  expect(screen.getByText('대기 중인 질문이 3개입니다. 답변 뒤에 보내 주세요.')).toBeTruthy()

  events.last.emit({
    type: 'limits',
    data: makeLimits({
      six_hour: { limit: 100, used: 100, reserved: 0, remaining: 0, resets_at: '2026-10-20T09:00:00Z' },
    }),
  })
  expect(sendButton().disabled).toBe(true)
  expect(screen.getByText('질문 한도를 모두 썼습니다. 다음 초기화: 오후 6:00')).toBeTruthy()
})

test('10초 간격이 지나면 다시 그려 남은 초 안내를 지우고 보낼 수 있게 한다', async () => {
  const user = userEvent.setup()

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    return undefined
  })

  const { events } = renderChat()
  await findConversationButton('501')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  await user.type(textarea(), '질문')

  // 서버 시각 기준으로 1초 뒤에 보낼 수 있다.
  const now = new Date().toISOString()
  events.last.emit(snapshot([], makeLimits({ next_attempt_at: new Date(Date.parse(now) + 1000).toISOString() }), now))
  expect(screen.getByText(/초 뒤에 보낼 수 있습니다/)).toBeTruthy()
  expect(sendButton().disabled).toBe(true)

  await waitFor(() => expect(sendButton().disabled).toBe(false), { timeout: 3000 })
  expect(screen.queryByText(/초 뒤에 보낼 수 있습니다/)).toBeNull()
})

test('429 거절은 서버가 알려 준 남은 초를 보여 주고 입력을 잠그지 않는다', async () => {
  const user = userEvent.setup()

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(
        429,
        { error: { code: 'attempt_too_soon', message: '잠시 후 다시 보내 주세요.' } },
        { 'Retry-After': '7' },
      )
    }
    return undefined
  })

  renderChat()
  await findConversationButton('501')
  await user.type(textarea(), '질문')
  await user.click(sendButton())

  expect((await screen.findByRole('alert')).textContent).toBe('7초 뒤에 보낼 수 있습니다.대화 다시 불러오기')
  expect(textarea().disabled).toBe(false)
})

test('성공한 뒤의 새 질문은 새 request_key를 쓴다', async () => {
  const user = userEvent.setup()
  let jobId = 7000

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      jobId += 1
      return acceptQuestion('501', init, String(jobId), String(jobId + 2000))
    }
    return undefined
  })

  renderChat()
  await findConversationButton('501')

  for (let round = 0; round < 2; round += 1) {
    await user.type(textarea(), '같은 질문')
    await user.click(sendButton())
    await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(round + 1))
    await waitFor(() => expect(textarea().value).toBe(''))
  }

  const [first, second] = questionPosts(fetchSpy)
  expect(second?.body.request_key).not.toBe(first?.body.request_key)
  expect(screen.getAllByText('같은 질문')).toHaveLength(2)
})

test('다른 대화로 옮긴 뒤 같은 문장을 보내면 이전 대화의 키를 쓰지 않고 선택한 대화로 보낸다', async () => {
  const user = userEvent.setup()
  let posts = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url.startsWith('/api/conversations/502/messages?') || url.startsWith('/api/conversations/501/messages?')) {
      return json(200, page([]))
    }
    if (url.endsWith('/messages') && init?.method === 'POST') {
      posts += 1
      if (posts === 1) return json(503, { error: { message: '일시적인 오류입니다.' } })
      return acceptQuestion('501', init)
    }
    return undefined
  })

  renderChat()
  await findConversationButton('502')

  await user.type(textarea(), '같은 문장')
  await user.click(sendButton())
  await screen.findByRole('alert')

  await user.click(conversationButton('501'))
  await waitFor(() => expect(sendButton().textContent).toBe('질문 보내기'))
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(2))

  const [first, second] = questionPosts(fetchSpy)
  expect(first?.url).toBe('/api/conversations/502/messages')
  expect(second?.url).toBe('/api/conversations/501/messages')
  expect(second?.body.request_key).not.toBe(first?.body.request_key)
})

test('응답을 잃은 질문은 다른 대화를 다녀온 뒤에도 같은 request_key로 다시 보낸다', async () => {
  const user = userEvent.setup()
  let postsToA = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url.startsWith('/api/conversations/502/messages?') || url.startsWith('/api/conversations/501/messages?')) {
      return json(200, page([]))
    }
    if (url === '/api/conversations/502/messages' && init?.method === 'POST') {
      postsToA += 1
      if (postsToA === 1) return json(503, { error: { message: '일시적인 오류입니다.' } })
      return acceptQuestion('502', init, '7002', '9003')
    }
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') return acceptQuestion('501', init)
    return undefined
  })

  renderChat()
  await findConversationButton('502')

  // A(502)에서 응답을 받지 못한다.
  await user.type(textarea(), 'A 질문')
  await user.click(sendButton())
  await screen.findByRole('alert')

  // B(501)에서 다른 질문을 보내 성공한다.
  await user.click(conversationButton('501'))
  await user.clear(textarea())
  await user.type(textarea(), 'B 질문')
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(2))
  await waitFor(() => expect(textarea().value).toBe(''))

  // 다시 A로 돌아와 같은 질문을 보내면 처음 키를 쓴다.
  await user.click(conversationButton('502'))
  await user.type(textarea(), 'A 질문')
  expect(sendButton().textContent).toBe('다시 확인하기')
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(3))

  const [firstA, , secondA] = questionPosts(fetchSpy)
  expect(secondA?.url).toBe('/api/conversations/502/messages')
  expect(secondA?.body.request_key).toBe(firstA?.body.request_key)
  expect(secondA?.body.content).toBe('A 질문')
})

test('#53 이전에 저장된 질문을 같은 키로 다시 보내 job 없이 답변만 오면 이력을 다시 읽어 보여 준다', async () => {
  const user = userEvent.setup()
  let reads = 0

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      reads += 1
      return json(
        200,
        reads === 1
          ? page([])
          : page([userMessage('1', '예전 질문'), assistantMessage('2', '1', 'completed', '예전에 저장된 답')]),
      )
    }
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(200, {
        question: userMessage('1', '예전 질문'),
        answer: assistantMessage('2', '1', 'completed', '예전에 저장된 답'),
        job: null,
        limits: makeLimits(),
      })
    }
    return undefined
  })

  renderChat()
  await findConversationButton('501')
  await user.type(textarea(), '예전 질문')
  await user.click(sendButton())

  expect(await screen.findByText('예전에 저장된 답')).toBeTruthy()
})

// --- 재시도·건너뛰기·취소 ---

/** 대화 501: 9003 실패(7002) 뒤 9005가 정지(7003)된 상태 */
function failedConversation() {
  const history = [
    userMessage('9003', '음주운전 단속 기준은?'),
    assistantMessage('9004', '9003', 'failed', null, 'provider_failed'),
    userMessage('9005', '전동킥보드 헬멧 범칙금은?'),
  ]
  const failed = makeJob({
    id: '7002',
    question_message_id: '9003',
    answer_message_id: '9004',
    state: 'failed',
    position: null,
    error_code: 'provider_failed',
    allowed_actions: ['retry', 'skip'],
  })
  const paused = makeJob({
    id: '7003',
    question_message_id: '9005',
    state: 'paused',
    position: null,
    paused_by_message_id: '9003',
    allowed_actions: ['cancel'],
  })
  return { history, failed, paused }
}

test('새로고침 뒤에도 대화의 작업 목록으로 실패 답변의 재시도·건너뛰기 버튼을 되살린다', async () => {
  const { history, failed, paused } = failedConversation()

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page(history))
    if (url === '/api/chat/jobs?conversation_id=501&limit=100') return json(200, jobPage([paused, failed]))
    return undefined
  })

  renderChat()

  expect(await screen.findByRole('button', { name: '재시도' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '건너뛰기' })).toBeTruthy()
  expect(screen.getByText('답변을 만들지 못했습니다. (오류 코드: provider_failed)')).toBeTruthy()
  expect(screen.getByText('앞 질문 실패로 대기 중')).toBeTruthy()
  expect(screen.getByRole('button', { name: '질문 취소' })).toBeTruthy()
})

test('재시도 응답을 잃으면 다시 눌러도 같은 retry_key로 보내고, 접수되면 원래 실패 답변의 버튼을 숨긴다', async () => {
  const user = userEvent.setup()
  const { history, failed, paused } = failedConversation()
  let retries = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page(history))
    if (url === '/api/chat/jobs?conversation_id=501&limit=100') return json(200, jobPage([paused, failed]))
    if (url === '/api/chat/jobs/7002') {
      return json(200, { job: failed, question: history[0], answer: history[1] })
    }
    if (url === '/api/conversations/501/messages/9003/retry' && init?.method === 'POST') {
      retries += 1
      if (retries === 1) throw new TypeError('Failed to fetch')
      return json(
        202,
        admission({
          job: makeJob({ id: '7005', kind: 'retry', question_message_id: '9003', position: 1 }),
          question: history[0] as ApiMessage,
          answer: null,
        }),
      )
    }
    return undefined
  })

  const { events } = renderChat()
  await user.click(await screen.findByRole('button', { name: '재시도' }))

  expect((await screen.findByRole('alert')).textContent).toContain('다시 누르면 같은 요청으로 확인합니다.')

  await user.click(screen.getByRole('button', { name: '재시도' }))
  await waitFor(() => expect(retries).toBe(2))

  const [first, second] = postCalls(fetchSpy).filter((call) => call.url.endsWith('/retry'))
  expect(second?.body.retry_key).toBe(first?.body.retry_key)

  // 재시도 작업이 바로 보인다. 원래 실패 작업의 버튼은 서버가 job 이벤트로 바꿔 준다.
  expect(await screen.findByText('재시도 · 대기 중 · 1번째')).toBeTruthy()
  events.last.emit({
    type: 'job',
    data: { job: { ...failed, allowed_actions: [] }, question: history[0] as ApiMessage, answer: history[1] ?? null },
  })
  expect(screen.queryByRole('button', { name: '재시도' })).toBeNull()
  expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull()
  expect(screen.getByText('재시도 대기 중 · 취소하면 건너뛸 수 있습니다')).toBeTruthy()
  expect(screen.getByRole('button', { name: '재시도 취소' })).toBeTruthy()
})

test('건너뛰면 멈췄던 뒤 질문을 다시 대기열에 넣고, 실패 작업의 버튼을 다시 읽는다', async () => {
  const user = userEvent.setup()
  const { history, failed, paused } = failedConversation()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page(history))
    if (url === '/api/chat/jobs?conversation_id=501&limit=100') return json(200, jobPage([paused, failed]))
    if (url === '/api/conversations/501/messages/9003/skip' && init?.method === 'POST') {
      return json(200, {
        skipped_message_id: '9003',
        resumed_jobs: [{ ...paused, state: 'queued', position: 1, paused_by_message_id: null }],
        limits: makeLimits(),
      })
    }
    if (url === '/api/chat/jobs/7002') {
      return json(200, { job: { ...failed, allowed_actions: ['retry'] }, question: history[0], answer: history[1] })
    }
    return undefined
  })

  renderChat()
  await user.click(await screen.findByRole('button', { name: '건너뛰기' }))

  expect(await screen.findByText('대기 중 · 1번째')).toBeTruthy()
  await waitFor(() => expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull())
  expect(screen.getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(postCalls(fetchSpy).map((call) => call.url)).toEqual(['/api/conversations/501/messages/9003/skip'])
})

test('실행 전 질문을 취소하면 취소한 질문으로 남고, 거절되면 그 작업을 다시 읽어 화면을 맞춘다', async () => {
  const user = userEvent.setup()
  const queued = makeEnvelope({ question: { content: '취소할 질문' } })

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([queued.question]))
    if (url === '/api/chat/jobs/7001/cancel' && init?.method === 'POST') {
      return json(200, {
        job: { ...queued.job, state: 'canceled', position: null, allowed_actions: [] },
        limits: makeLimits(),
      })
    }
    return undefined
  })

  const { events } = renderChat()
  await screen.findByText('취소할 질문')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot([queued]))

  await user.click(screen.getByRole('button', { name: '질문 취소' }))

  expect(await screen.findByText('취소한 질문')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '질문 취소' })).toBeNull()
})

test('실행이 시작돼 취소가 거절되면 안내를 보이고 그 작업을 다시 읽어 버튼을 지운다', async () => {
  const user = userEvent.setup()
  const queued = makeEnvelope({ question: { content: '이미 시작된 질문' } })

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([queued.question]))
    if (url === '/api/chat/jobs/7001/cancel' && init?.method === 'POST') {
      return json(409, {
        error: { code: 'job_not_cancelable', message: '이미 실행 중이거나 끝난 질문은 취소할 수 없습니다.' },
      })
    }
    if (url === '/api/chat/jobs/7001') {
      return json(200, {
        job: { ...queued.job, state: 'running', position: null, allowed_actions: [] },
        question: queued.question,
        answer: makeMessage({ id: '9002', role: 'assistant', status: 'pending', reply_to_message_id: '9001' }),
      })
    }
    return undefined
  })

  const { events } = renderChat()
  await screen.findByText('이미 시작된 질문')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot([queued]))

  await user.click(screen.getByRole('button', { name: '질문 취소' }))

  expect(await screen.findByText('답변 생성 중입니다.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '질문 취소' })).toBeNull()
})

// --- 회원 전환·로그아웃·화면 떠나기 ---

test('회원이 바뀌면 다시 그린 첫 화면부터 이전 회원의 기록·임시 출력이 없고 이전 구독은 끊긴다', async () => {
  mockApi((url, init) => {
    if (authHeader(init) === 'Bearer token-b') {
      if (url === '/api/conversations?limit=20') return json(200, page([conversation('601')]))
      if (url === '/api/conversations/601/messages?limit=100') return json(200, page([userMessage('8', 'Q-USER-B')]))
      return undefined
    }
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('9001', 'Q-USER-A')]))
    return undefined
  })

  const { rerender, events } = renderChat()
  await screen.findByText('Q-USER-A')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  const connectionA = events.last
  connectionA.emit(snapshot())
  connectionA.emit({
    type: 'job',
    data: makeEnvelope({
      job: { state: 'running', position: null, allowed_actions: [] },
      question: { content: 'Q-USER-A' },
      answer: { id: '9002' },
    }),
  })
  connectionA.emit({ type: 'delta', data: { job_id: '7001', text: 'A의 임시 출력' } })
  fireEvent.change(textarea(), { target: { value: 'A가 쓰던 초안' } })
  expect(screen.getByText('A의 임시 출력')).toBeTruthy()

  auth.value = { accessToken: 'token-b', user: { id: 'u2' } }
  rerender()

  // 응답을 기다리기 전, 다시 그린 바로 그 화면을 확인한다.
  expect(screen.queryByText('Q-USER-A')).toBeNull()
  expect(screen.queryByText('A의 임시 출력')).toBeNull()
  expect(screen.queryByRole('button', { name: conversationLabel(conversation('501')) })).toBeNull()
  expect(textarea().value).toBe('')
  expect(connectionA.signal.aborted).toBe(true)

  // 끊긴 A의 구독으로 늦게 온 이벤트도 B의 화면에 나타나지 않는다.
  connectionA.emit({ type: 'delta', data: { job_id: '7001', text: 'A의 늦은 조각' } })

  expect(await screen.findByText('Q-USER-B')).toBeTruthy()
  await waitFor(() => expect(events.last.accessToken).toBe('token-b'))
  expect(screen.queryByText('A의 늦은 조각')).toBeNull()
})

test('계정이 바뀐 뒤 새 목록 조회가 실패해도 이전 사용자의 대화와 입력 내용이 남지 않는다', async () => {
  const user = userEvent.setup()

  mockApi((url, init) => {
    if (authHeader(init) === 'Bearer token-b') {
      if (url === '/api/conversations?limit=20') return json(500, { error: { message: '목록을 불러오지 못했습니다.' } })
      return undefined
    }
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('1', 'Q-USER-A')]))
    return undefined
  })

  const { rerender } = renderChat()
  await screen.findByText('Q-USER-A')
  await user.type(textarea(), 'A가 쓰던 초안')

  auth.value = { accessToken: 'token-b', user: { id: 'u2' } }
  rerender()

  expect((await screen.findByRole('alert')).textContent).toContain('목록을 불러오지 못했습니다.')
  expect(screen.queryByText('Q-USER-A')).toBeNull()
  expect(screen.queryByRole('button', { name: conversationLabel(conversation('501')) })).toBeNull()
  expect(textarea().value).toBe('')
})

test('전송 중에 계정이 바뀌면 입력창이 잠긴 채로 남지 않고, 늦은 전송 응답이 새 계정 화면을 건드리지 않는다', async () => {
  const user = userEvent.setup()
  const slowPost = deferred<Response | undefined>()
  let postInit: RequestInit | undefined

  const fetchSpy = mockApi((url, init) => {
    if (authHeader(init) === 'Bearer token-b') {
      if (url === '/api/conversations?limit=20') return json(200, page([conversation('601')]))
      if (url === '/api/conversations/601/messages?limit=100') return json(200, page([userMessage('9', 'Q-USER-B')]))
      if (url.startsWith('/api/chat/jobs?conversation_id=601')) return json(200, jobPage([]))
      return undefined
    }
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      postInit = init
      return slowPost.promise
    }
    return undefined
  })

  const { rerender } = renderChat()
  await findConversationButton('501')

  await user.type(textarea(), 'A의 질문')
  await user.click(sendButton())
  expect(await screen.findByRole('button', { name: '전송 중...' })).toBeTruthy()

  auth.value = { accessToken: 'token-b', user: { id: 'u2' } }
  rerender()

  await screen.findByText('Q-USER-B')
  expect(textarea().disabled).toBe(false)
  expect(textarea().value).toBe('')
  expect(screen.queryByRole('button', { name: '전송 중...' })).toBeNull()
  // 화면을 떠나면 진행 중인 요청을 끊는다. 서버에 접수된 작업은 그대로다.
  expect(postInit?.signal?.aborted).toBe(true)

  // 이전 계정의 전송 응답이 뒤늦게 도착해도 새 계정 화면과 요청에 영향이 없다.
  const callsBefore = fetchSpy.mock.calls.length
  slowPost.resolve(acceptQuestion('501', postInit))
  await settle()
  expect(screen.queryByText('A의 질문')).toBeNull()
  expect(screen.getByText('Q-USER-B')).toBeTruthy()
  expect(fetchSpy.mock.calls).toHaveLength(callsBefore)
})

test('로그아웃하면 구독을 끊고 화면을 비우지만, 서버 작업을 취소하는 요청은 보내지 않는다', async () => {
  const fetchSpy = mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('9001', 'Q-PRIVATE')]))
    return undefined
  })

  const { rerender, events } = renderChat()
  await screen.findByText('Q-PRIVATE')
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot([makeEnvelope({ question: { content: 'Q-PRIVATE' } })]))

  auth.value = { accessToken: null, user: null }
  rerender()
  await settle()

  expect(screen.queryByText('Q-PRIVATE')).toBeNull()
  expect(events.last.signal.aborted).toBe(true)
  expect(events.connections).toHaveLength(1)
  expect(postCalls(fetchSpy)).toEqual([])
})

test('화면을 떠난 뒤 늦게 끝난 조회는 화면을 바꾸거나 새 요청·재연결을 만들지 않는다', async () => {
  const slowJobs = deferred<Response | undefined>()
  const slowLookup = deferred<Response | undefined>()

  const fetchSpy = mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('9001', '질문')]))
    if (url === '/api/chat/jobs?conversation_id=501&limit=100') return slowJobs.promise
    if (url === '/api/chat/jobs/7001') return slowLookup.promise
    return undefined
  })

  const { unmount, events } = renderChat()
  await waitFor(() => expect(events.connections).toHaveLength(1))
  events.last.emit(snapshot([makeEnvelope({ job: { state: 'running', position: null, allowed_actions: [] } })]))
  // 진행 중이던 작업이 다음 snapshot에서 빠져 최종 결과를 따로 조회한다.
  events.last.emit(snapshot([]))
  await waitFor(() => expect(fetchSpy.mock.calls.some(([input]) => String(input) === '/api/chat/jobs/7001')).toBe(true))

  unmount()
  const callsBefore = fetchSpy.mock.calls.length

  slowJobs.resolve(json(200, jobPage([])))
  slowLookup.resolve(json(500, { error: { message: '실패' } }))
  // 조회를 다시 시도하는 간격(10ms)보다 충분히 기다린다.
  await settle(80)

  expect(fetchSpy.mock.calls).toHaveLength(callsBefore)
  expect(events.connections).toHaveLength(1)
  expect(fetchSpy.unexpected).toEqual([])
})

// --- 입력 규칙 ---

test('첫 대화 목록을 읽는 동안 새 대화를 만들면, 늦게 도착한 목록이 새 대화와 현재 선택을 덮어쓰지 않는다', async () => {
  const user = userEvent.setup()
  const slowList = deferred<Response | undefined>()

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return slowList.promise
    if (url === '/api/conversations' && init?.method === 'POST') return json(201, { conversation: conversation('700') })
    if (url === '/api/conversations/700/messages' && init?.method === 'POST') {
      return acceptQuestion('700', init, '7001', '9001')
    }
    return undefined
  })

  renderChat()

  // 목록 응답이 오기 전에 첫 질문을 보내 새 대화를 만든다.
  await user.type(textarea(), '새 질문')
  await user.click(sendButton())
  expect(await screen.findByText('새 질문')).toBeTruthy()
  expect((await findConversationButton('700')).getAttribute('aria-current')).toBe('page')

  // 이제야 이전 목록이 도착한다. 서버 목록에는 방금 만든 대화가 아직 없다.
  slowList.resolve(json(200, page([conversation('501')])))
  await findConversationButton('501')

  expect(conversationButton('700').getAttribute('aria-current')).toBe('page')
  expect(screen.getByText('새 질문')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})

test('질문 앞뒤 공백은 그대로 보내고, 공백뿐인 질문은 보낼 수 없다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') return acceptQuestion('501', init)
    return undefined
  })

  renderChat()
  await findConversationButton('501')

  fireEvent.change(textarea(), { target: { value: '   \n  ' } })
  expect(sendButton().disabled).toBe(true)

  fireEvent.change(textarea(), { target: { value: '\n  들여쓴 질문  \n' } })
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(1))

  expect(questionPosts(fetchSpy)[0]?.body.content).toBe('\n  들여쓴 질문  \n')
})

test('질문 길이는 Unicode 코드 포인트 4,000자까지 허용하고, 이모지도 일찍 잘리지 않는다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') return acceptQuestion('501', init)
    return undefined
  })

  renderChat()
  await findConversationButton('501')

  const input = textarea()

  // 이모지 하나는 UTF-16으로 2칸이라 4,000개면 .length는 8,000이다.
  const atLimit = '😀'.repeat(4000)
  fireEvent.change(input, { target: { value: atLimit } })
  expect(input.value).toBe(atLimit)
  expect(sendButton().disabled).toBe(false)

  fireEvent.change(input, { target: { value: `${atLimit}😀` } })
  expect(sendButton().disabled).toBe(true)
  expect(screen.getByText(/4,000자까지 보낼 수 있습니다/)).toBeTruthy()
  expect(questionPosts(fetchSpy)).toHaveLength(0)

  fireEvent.change(input, { target: { value: atLimit } })
  await user.click(sendButton())
  await waitFor(() => expect(questionPosts(fetchSpy)).toHaveLength(1))
  expect(Array.from(questionPosts(fetchSpy)[0]?.body.content ?? '')).toHaveLength(4000)
})

test('대화가 없을 때 질문을 연달아 제출해도 대화는 하나만 만들고 질문도 한 번만 보낸다', async () => {
  const slowCreate = deferred<Response | undefined>()
  let createCalls = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([]))
    if (url === '/api/conversations' && init?.method === 'POST') {
      createCalls += 1
      return slowCreate.promise
    }
    if (url === '/api/conversations/700/messages' && init?.method === 'POST') return acceptQuestion('700', init)
    return undefined
  })

  renderChat()
  await screen.findByText('대화 기록이 아직 없어요.')

  const input = textarea()
  fireEvent.change(input, { target: { value: '첫 질문' } })

  // 화면이 다시 그려지기 전에 같은 폼을 두 번 제출한다(엔터 연타·더블클릭). 한 번의 act 안에서 보내야 갱신이 끼어들지 않는다.
  const form = input.closest('form') as HTMLFormElement
  act(() => {
    form.requestSubmit()
    form.requestSubmit()
  })

  slowCreate.resolve(json(201, { conversation: conversation('700') }))
  await screen.findByText('첫 질문')

  expect(createCalls).toBe(1)
  expect(questionPosts(fetchSpy)).toHaveLength(1)
})

test('질문을 보내는 동안에는 사이드바에서 대화를 바꾸거나 새 대화를 시작할 수 없다', async () => {
  const user = userEvent.setup()
  const slowPost = deferred<Response | undefined>()
  let postInit: RequestInit | undefined

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url.startsWith('/api/conversations/502/messages?') || url.startsWith('/api/conversations/501/messages?')) {
      return json(200, page([]))
    }
    if (url === '/api/conversations/502/messages' && init?.method === 'POST') {
      postInit = init
      return slowPost.promise
    }
    return undefined
  })

  renderChat()
  await findConversationButton('502')

  await user.type(textarea(), '질문')
  await user.click(sendButton())

  const sidebar = screen.getByRole('navigation', { name: '대화 기록' })
  const other = within(sidebar).getByRole('button', {
    name: conversationLabel(conversation('501')),
  }) as HTMLButtonElement
  await waitFor(() => expect(other.disabled).toBe(true))
  expect((screen.getByRole('button', { name: '＋ 새 대화' }) as HTMLButtonElement).disabled).toBe(true)

  slowPost.resolve(acceptQuestion('502', postInit))
  await waitFor(() => expect(other.disabled).toBe(false))
})
