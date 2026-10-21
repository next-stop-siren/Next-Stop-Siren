// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
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

type Handler = (url: string, init?: RequestInit) => Response | undefined

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

function mockApi(handler: Handler) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    const response = handler(url, init)
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
  expect(first.body.content).toBe('같은 질문')
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
