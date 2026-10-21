// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MemberChat from './MemberChat'
import type { ApiMessage } from './memberChatApiTypes'

// MemberChat은 인증 상태만 읽는다. 로그인 절차는 AuthContext 테스트가 다루므로 값을 직접 주입한다.
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

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

function mockApi(handler: Handler) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    const response = await handler(url, init)
    if (response) return response
    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${init?.method ?? 'GET'} ${url}`)
  })
}

function userMessage(id: string, content: string, requestKey = `key-${id}`): ApiMessage {
  return {
    id,
    role: 'user',
    status: 'completed',
    content,
    request_key: requestKey,
    reply_to_message_id: null,
    attempt_no: null,
    error_code: null,
    created_at: '2026-10-21T00:00:00Z',
    updated_at: '2026-10-21T00:00:00Z',
    completed_at: '2026-10-21T00:00:00Z',
  }
}

function assistantMessage(
  id: string,
  replyTo: string,
  status: ApiMessage['status'],
  content: string | null,
  errorCode: string | null = null,
): ApiMessage {
  return {
    id,
    role: 'assistant',
    status,
    content,
    request_key: null,
    reply_to_message_id: replyTo,
    attempt_no: 1,
    error_code: errorCode,
    created_at: '2026-10-21T00:00:01Z',
    updated_at: '2026-10-21T00:00:01Z',
    completed_at: status === 'completed' ? '2026-10-21T00:00:01Z' : null,
  }
}

function conversation(id: string) {
  return { id, created_at: '2026-10-21T00:00:00Z', updated_at: '2026-10-21T00:00:00Z' }
}

function page(items: unknown[], nextCursor: string | null = null) {
  return { items, next_cursor: nextCursor }
}

function postCalls(fetchSpy: ReturnType<typeof mockApi>) {
  return fetchSpy.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([input, init]) => ({
      url: String(input),
      body: JSON.parse(String(init?.body)) as { content: string; request_key: string },
    }))
}

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

  render(<MemberChat />)

  expect(await screen.findByText('A-LAST')).toBeTruthy()
  expect(screen.getByText('A-FIRST')).toBeTruthy()
})

test('대화 목록도 next_cursor를 따라 모두 읽는다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('503')], '503'))
    if (url === '/api/conversations?limit=20&before_id=503') return json(200, page([conversation('502')]))
    if (url === '/api/conversations/503/messages?limit=100') return json(200, page([]))
    return undefined
  })

  render(<MemberChat />)

  expect(await screen.findByRole('option', { name: '대화 502' })).toBeTruthy()
  expect(screen.getByRole('option', { name: '대화 503' })).toBeTruthy()
})

test('access 토큰이 갱신돼도 선택한 대화가 유지되고 목록을 다시 읽지 않는다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('502'), conversation('501')]))
    if (url === '/api/conversations/502/messages?limit=100') return json(200, page([userMessage('1', 'Q-502')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('2', 'Q-501')]))
    return undefined
  })

  const { rerender } = render(<MemberChat />)

  await screen.findByText('Q-502')
  await user.selectOptions(screen.getByRole('combobox', { name: '대화 선택' }), '501')
  await screen.findByText('Q-501')

  // 토큰만 새 값으로 바뀐다. 사용자는 그대로다.
  auth.value = { accessToken: 'token-2', user: { id: 'u1' } }
  rerender(<MemberChat />)

  await waitFor(() =>
    expect((screen.getByRole('combobox', { name: '대화 선택' }) as HTMLSelectElement).value).toBe('501'),
  )
  expect(screen.getByText('Q-501')).toBeTruthy()

  const listCalls = fetchSpy.mock.calls.filter(([input]) => String(input) === '/api/conversations?limit=20')
  expect(listCalls).toHaveLength(1)
})

test('토큰 갱신 뒤의 요청은 새 토큰을 쓴다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      return init?.method === 'POST' ? undefined : json(200, page([]))
    }
    if (url === '/api/conversations/501/messages') {
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  const { rerender } = render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  auth.value = { accessToken: 'token-2', user: { id: 'u1' } }
  rerender(<MemberChat />)

  await user.type(screen.getByLabelText('질문 입력'), '질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))

  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(1))

  const post = fetchSpy.mock.calls.find(([, init]) => init?.method === 'POST')
  const headers = new Headers(post?.[1]?.headers)
  expect(headers.get('Authorization')).toBe('Bearer token-2')
})

test('전송이 실패하면 다시 보낼 때 같은 request_key와 본문을 쓴다', async () => {
  const user = userEvent.setup()
  let posts = 0

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      posts += 1
      if (posts === 1) return json(503, { error: { code: 'temporarily_unavailable', message: '일시적인 오류입니다.' } })
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  await user.type(screen.getByLabelText('질문 입력'), '  같은 질문  ')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))

  expect((await screen.findByRole('alert')).textContent).toContain('일시적인 오류입니다.')

  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(2))

  const [first, second] = postCalls(fetchSpy)
  expect(second.body.request_key).toBe(first.body.request_key)
  expect(second.body.content).toBe(first.body.content)
  // 서버는 질문 원문을 보존하므로 앞뒤 공백도 그대로 보낸다.
  expect(first.body.content).toBe('  같은 질문  ')
})

test('성공한 뒤의 새 질문은 새 request_key를 쓴다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  for (let round = 0; round < 2; round += 1) {
    await user.type(screen.getByLabelText('질문 입력'), '같은 질문')
    await user.click(screen.getByRole('button', { name: '질문 보내기' }))
    await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(round + 1))
    await waitFor(() => expect((screen.getByLabelText('질문 입력') as HTMLTextAreaElement).value).toBe(''))
  }

  const [first, second] = postCalls(fetchSpy)
  expect(second.body.request_key).not.toBe(first.body.request_key)
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
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 502' })

  await user.type(screen.getByLabelText('질문 입력'), '같은 문장')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await screen.findByRole('alert')

  await user.selectOptions(screen.getByRole('combobox', { name: '대화 선택' }), '501')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(2))

  const [first, second] = postCalls(fetchSpy)
  expect(first.url).toBe('/api/conversations/502/messages')
  expect(second.url).toBe('/api/conversations/501/messages')
  expect(second.body.request_key).not.toBe(first.body.request_key)
})

test('실패·중단·진행 중 답변은 내용이 있어도 완료 답변으로 보이지 않는다', async () => {
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

  render(<MemberChat />)

  expect(await screen.findByText('FINAL-ANSWER')).toBeTruthy()
  expect(screen.queryByText('PARTIAL-FAILED')).toBeNull()
  expect(screen.queryByText('PARTIAL-INTERRUPTED')).toBeNull()
  expect(screen.queryByText('PARTIAL-PENDING')).toBeNull()
  expect(screen.getByText('생성 실패')).toBeTruthy()
  expect(screen.getByText('응답 중단')).toBeTruthy()
  expect(screen.getByText('답변 대기 중')).toBeTruthy()
})

test('진행 중 답변은 새로고침 버튼으로 저장된 상태를 다시 읽을 수 있다', async () => {
  const user = userEvent.setup()
  let reads = 0

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      reads += 1
      return json(
        200,
        page([
          userMessage('1', 'Q-1'),
          reads === 1
            ? assistantMessage('2', '1', 'pending', null)
            : assistantMessage('2', '1', 'completed', 'DONE-LATER'),
        ]),
      )
    }
    return undefined
  })

  render(<MemberChat />)

  await user.click(await screen.findByRole('button', { name: '답변 상태 새로고침' }))

  expect(await screen.findByText('DONE-LATER')).toBeTruthy()
})

test('로그아웃해 사용자가 없어지면 대화와 입력 상태를 비운다', async () => {
  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([userMessage('1', 'Q-PRIVATE')]))
    return undefined
  })

  const { rerender } = render(<MemberChat />)
  await screen.findByText('Q-PRIVATE')

  auth.value = { accessToken: null, user: null }
  rerender(<MemberChat />)

  await waitFor(() => expect(screen.queryByText('Q-PRIVATE')).toBeNull())
  expect(screen.queryByRole('option', { name: '대화 501' })).toBeNull()
})

function authHeader(init?: RequestInit): string {
  return new Headers(init?.headers).get('Authorization') ?? ''
}

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

  const { rerender } = render(<MemberChat />)
  await screen.findByText('Q-USER-A')
  await user.type(screen.getByLabelText('질문 입력'), 'A가 쓰던 초안')

  auth.value = { accessToken: 'token-b', user: { id: 'u2' } }
  rerender(<MemberChat />)

  expect((await screen.findByRole('alert')).textContent).toContain('목록을 불러오지 못했습니다.')
  expect(screen.queryByText('Q-USER-A')).toBeNull()
  expect(screen.queryByRole('option', { name: '대화 501' })).toBeNull()
  expect((screen.getByLabelText('질문 입력') as HTMLTextAreaElement).value).toBe('')
})

test('전송 중에 계정이 바뀌면 입력창이 잠긴 채로 남지 않고, 늦은 전송 응답이 새 계정 화면을 건드리지 않는다', async () => {
  const user = userEvent.setup()
  const slowPost = deferred<Response | undefined>()

  const fetchSpy = mockApi((url, init) => {
    if (authHeader(init) === 'Bearer token-b') {
      if (url === '/api/conversations?limit=20') return json(200, page([conversation('601')]))
      if (url === '/api/conversations/601/messages?limit=100') return json(200, page([userMessage('9', 'Q-USER-B')]))
      return undefined
    }
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') return slowPost.promise
    return undefined
  })

  const { rerender } = render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  await user.type(screen.getByLabelText('질문 입력'), 'A의 질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  expect(await screen.findByRole('button', { name: '전송 중...' })).toBeTruthy()

  auth.value = { accessToken: 'token-b', user: { id: 'u2' } }
  rerender(<MemberChat />)

  await screen.findByText('Q-USER-B')
  const input = screen.getByLabelText('질문 입력') as HTMLTextAreaElement
  expect(input.disabled).toBe(false)
  expect(input.value).toBe('')
  expect(screen.queryByRole('button', { name: '전송 중...' })).toBeNull()

  // 이전 계정의 전송 응답이 뒤늦게 도착해도 새 계정 화면과 요청에 영향이 없다.
  const callsBefore = fetchSpy.mock.calls.length
  slowPost.resolve(
    json(201, { question: userMessage('1', 'A의 질문'), answer: assistantMessage('2', '1', 'completed', 'A의 답') }),
  )
  await waitFor(() => expect(screen.queryByText('A의 답')).toBeNull())
  expect(screen.getByText('Q-USER-B')).toBeTruthy()
  expect(fetchSpy.mock.calls).toHaveLength(callsBefore)
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
      return json(201, { question: userMessage('1', 'A 질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(201, { question: userMessage('3', 'B 질문'), answer: assistantMessage('4', '3', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 502' })

  // A(502)에서 응답을 받지 못한다.
  await user.type(screen.getByLabelText('질문 입력'), 'A 질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await screen.findByRole('alert')

  // B(501)에서 다른 질문을 보내 성공한다.
  await user.selectOptions(screen.getByRole('combobox', { name: '대화 선택' }), '501')
  await user.clear(screen.getByLabelText('질문 입력'))
  await user.type(screen.getByLabelText('질문 입력'), 'B 질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(2))
  await waitFor(() => expect((screen.getByLabelText('질문 입력') as HTMLTextAreaElement).value).toBe(''))

  // 다시 A로 돌아와 같은 질문을 보내면 처음 키를 쓴다.
  await user.selectOptions(screen.getByRole('combobox', { name: '대화 선택' }), '502')
  await user.type(screen.getByLabelText('질문 입력'), 'A 질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(3))

  const [firstA, , secondA] = postCalls(fetchSpy)
  expect(secondA.url).toBe('/api/conversations/502/messages')
  expect(secondA.body.request_key).toBe(firstA.body.request_key)
  expect(secondA.body.content).toBe('A 질문')
})

test('상태 조회가 겹쳐도 늦게 도착한 이전 응답이 완료된 답변을 다시 대기 상태로 되돌리지 않는다', async () => {
  const user = userEvent.setup()
  const slowRead = deferred<Response | undefined>()
  let reads = 0

  mockApi((url) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') {
      reads += 1
      if (reads === 1) return json(200, page([userMessage('1', 'Q-1'), assistantMessage('2', '1', 'pending', null)]))
      if (reads === 2) return slowRead.promise
      return json(200, page([userMessage('1', 'Q-1'), assistantMessage('2', '1', 'completed', 'DONE')]))
    }
    return undefined
  })

  render(<MemberChat />)

  // 첫 새로고침은 느리게 응답하고, 두 번째 새로고침은 먼저 완료 답변을 받는다.
  await user.click(await screen.findByRole('button', { name: '답변 상태 새로고침' }))
  await waitFor(() => expect(reads).toBe(2))
  await user.click(screen.getByRole('button', { name: '답변 상태 새로고침' }))
  expect(await screen.findByText('DONE')).toBeTruthy()

  // 먼저 시작했지만 늦게 도착한 응답은 진행 중 상태다.
  slowRead.resolve(json(200, page([userMessage('1', 'Q-1'), assistantMessage('2', '1', 'pending', null)])))
  await new Promise((resolve) => setTimeout(resolve, 20))

  expect(screen.getByText('DONE')).toBeTruthy()
  expect(screen.queryByText('답변 대기 중')).toBeNull()
})

test('첫 대화 목록을 읽는 동안 새 대화를 만들면, 늦게 도착한 목록이 새 대화와 현재 선택을 덮어쓰지 않는다', async () => {
  const user = userEvent.setup()
  const slowList = deferred<Response | undefined>()

  mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return slowList.promise
    if (url === '/api/conversations' && init?.method === 'POST') return json(201, { conversation: conversation('700') })
    if (url === '/api/conversations/700/messages' && init?.method === 'POST') {
      return json(201, {
        question: userMessage('1', '새 질문'),
        answer: assistantMessage('2', '1', 'completed', '새 답'),
      })
    }
    if (url === '/api/conversations/700/messages?limit=100') {
      return json(200, page([userMessage('1', '새 질문'), assistantMessage('2', '1', 'completed', '새 답')]))
    }
    return undefined
  })

  render(<MemberChat />)

  // 목록 응답이 오기 전에 첫 질문을 보내 새 대화를 만든다.
  await user.type(screen.getByLabelText('질문 입력'), '새 질문')
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  expect(await screen.findByText('새 답')).toBeTruthy()

  // 이제야 이전 목록이 도착한다. 서버 목록에는 방금 만든 대화가 아직 없다.
  slowList.resolve(json(200, page([conversation('501')])))
  await screen.findByRole('option', { name: '대화 501' })

  expect((screen.getByRole('combobox', { name: '대화 선택' }) as HTMLSelectElement).value).toBe('700')
  expect(screen.getByRole('option', { name: '대화 700' })).toBeTruthy()
  expect(screen.getByText('새 답')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
})

test('질문 앞뒤 공백은 그대로 보내고, 공백뿐인 질문은 보낼 수 없다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  fireEvent.change(screen.getByLabelText('질문 입력'), { target: { value: '   \n  ' } })
  expect((screen.getByRole('button', { name: '질문 보내기' }) as HTMLButtonElement).disabled).toBe(true)

  fireEvent.change(screen.getByLabelText('질문 입력'), { target: { value: '\n  들여쓴 질문  \n' } })
  await user.click(screen.getByRole('button', { name: '질문 보내기' }))
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(1))

  expect(postCalls(fetchSpy)[0]?.body.content).toBe('\n  들여쓴 질문  \n')
})

test('질문 길이는 Unicode 코드 포인트 4,000자까지 허용하고, 이모지도 일찍 잘리지 않는다', async () => {
  const user = userEvent.setup()

  const fetchSpy = mockApi((url, init) => {
    if (url === '/api/conversations?limit=20') return json(200, page([conversation('501')]))
    if (url === '/api/conversations/501/messages?limit=100') return json(200, page([]))
    if (url === '/api/conversations/501/messages' && init?.method === 'POST') {
      return json(201, { question: userMessage('1', '질문'), answer: assistantMessage('2', '1', 'completed', '답') })
    }
    return undefined
  })

  render(<MemberChat />)
  await screen.findByRole('option', { name: '대화 501' })

  const input = screen.getByLabelText('질문 입력') as HTMLTextAreaElement
  const send = screen.getByRole('button', { name: '질문 보내기' }) as HTMLButtonElement

  // 이모지 하나는 UTF-16으로 2칸이라 4,000개면 .length는 8,000이다.
  const atLimit = '😀'.repeat(4000)
  fireEvent.change(input, { target: { value: atLimit } })
  expect(input.value).toBe(atLimit)
  expect(send.disabled).toBe(false)

  fireEvent.change(input, { target: { value: `${atLimit}😀` } })
  expect(send.disabled).toBe(true)
  expect(screen.getByText(/4,000자까지 보낼 수 있습니다/)).toBeTruthy()
  expect(postCalls(fetchSpy)).toHaveLength(0)

  fireEvent.change(input, { target: { value: atLimit } })
  await user.click(send)
  await waitFor(() => expect(postCalls(fetchSpy)).toHaveLength(1))
  expect(Array.from(postCalls(fetchSpy)[0]?.body.content ?? '')).toHaveLength(4000)
})
