// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ChatJobActions from './ChatJobActions'
import { makeEnvelope, makeLimits } from './chatQueueBuilders'
import type { ChatJobAction, ChatJobEnvelope, ChatLimits } from './chatQueueTypes'
import type { ChatJobCommands } from './useChatJobCommands'

afterEach(() => {
  cleanup()
})

const NOW = Date.parse('2026-10-20T03:00:00Z')

function makeCommands(overrides: Partial<ChatJobCommands> = {}): ChatJobCommands {
  return {
    retry: vi.fn(async () => undefined),
    skip: vi.fn(async () => undefined),
    cancel: vi.fn(async () => undefined),
    ...overrides,
  }
}

function failed(actions: ChatJobAction[], id = '7001'): ChatJobEnvelope {
  return makeEnvelope({
    job: { id, state: 'failed', allowed_actions: actions, error_code: 'provider_failed' },
    answer: { id: '9002', status: 'failed', error_code: 'provider_failed' },
  })
}

function renderActions(
  envelope: ChatJobEnvelope,
  {
    jobs,
    limits = makeLimits(),
    commands = makeCommands(),
  }: { jobs?: Record<string, ChatJobEnvelope>; limits?: ChatLimits | null; commands?: ChatJobCommands } = {},
) {
  render(
    <ChatJobActions
      envelope={envelope}
      jobs={jobs ?? { [envelope.job.id]: envelope }}
      limits={limits}
      nowMs={NOW}
      commands={commands}
    />,
  )

  return commands
}

test('서버가 허용한 동작만 버튼으로 보인다', () => {
  renderActions(failed(['retry', 'skip']))

  expect(screen.getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '건너뛰기' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: /취소/ })).toBeNull()
})

test('건너뛴 뒤에는 재시도만 남는다', () => {
  renderActions(failed(['retry']))

  expect(screen.getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: '건너뛰기' })).toBeNull()
})

test('허용된 동작이 없으면 아무것도 그리지 않는다', () => {
  const envelope = failed([])
  const { container } = render(
    <ChatJobActions
      envelope={envelope}
      jobs={{ [envelope.job.id]: envelope }}
      limits={makeLimits()}
      nowMs={NOW}
      commands={makeCommands()}
    />,
  )

  expect(container.firstChild).toBeNull()
})

test('버튼을 누르면 그 작업 ID로 명령을 보낸다', async () => {
  const commands = renderActions(failed(['retry', 'skip']))

  fireEvent.click(screen.getByRole('button', { name: '재시도' }))
  expect(commands.retry).toHaveBeenCalledWith('7001')

  // 요청이 끝나 버튼이 다시 활성화된 뒤에 건너뛰기를 누른다.
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '건너뛰기' }) as HTMLButtonElement).disabled).toBe(false),
  )
  fireEvent.click(screen.getByRole('button', { name: '건너뛰기' }))
  expect(commands.skip).toHaveBeenCalledWith('7001')
})

test('질문 취소와 재시도 취소 버튼을 구분해서 보여 준다', () => {
  const queuedQuestion = makeEnvelope({
    job: { id: '7001', kind: 'question', state: 'queued', allowed_actions: ['cancel'] },
  })
  renderActions(queuedQuestion)
  expect(screen.getByRole('button', { name: '질문 취소' })).toBeTruthy()

  cleanup()

  const queuedRetry = makeEnvelope({ job: { id: '7002', kind: 'retry', state: 'queued', allowed_actions: ['cancel'] } })
  renderActions(queuedRetry)
  expect(screen.getByRole('button', { name: '재시도 취소' })).toBeTruthy()
})

test.each([
  [
    '한도 소진',
    makeLimits({ six_hour: { ...makeLimits().six_hour, remaining: 0 } }),
    '질문 한도를 모두 썼습니다. 다음 초기화: 오후 6:00',
  ],
  [
    '재시도 대기 가득',
    makeLimits({ queue: { ...makeLimits().queue, retries_waiting: 1 } }),
    '다른 재시도가 대기 중입니다.',
  ],
  ['10초 간격', makeLimits({ next_attempt_at: new Date(NOW + 4_000).toISOString() }), '4초 뒤에 보낼 수 있습니다'],
])('%s이면 재시도 버튼을 비활성화하고 이유를 보여 준다', (_name, limits, reason) => {
  const commands = renderActions(failed(['retry', 'skip']), { limits })

  const retry = screen.getByRole('button', { name: '재시도' }) as HTMLButtonElement
  expect(retry.disabled).toBe(true)
  expect(screen.getByText(reason)).toBeTruthy()
  expect(retry.getAttribute('aria-describedby')).toBe(screen.getByText(reason).id)

  fireEvent.click(retry)
  expect(commands.retry).not.toHaveBeenCalled()

  // 건너뛰기는 한도와 무관하다.
  expect((screen.getByRole('button', { name: '건너뛰기' }) as HTMLButtonElement).disabled).toBe(false)
})

test('새 질문 대기가 가득 차도 재시도 버튼은 막지 않는다', () => {
  renderActions(failed(['retry', 'skip']), {
    limits: makeLimits({ queue: { ...makeLimits().queue, questions_waiting: 3 } }),
  })

  expect((screen.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(false)
})

test('한도 때문에 막혀도 서버가 허용하지 않은 버튼은 새로 만들지 않는다', () => {
  renderActions(failed([]), { limits: makeLimits({ six_hour: { ...makeLimits().six_hour, remaining: 0 } }) })

  expect(screen.queryByRole('button', { name: '재시도' })).toBeNull()
  expect(screen.queryByText(/질문 한도/)).toBeNull()
})

test('재시도가 대기 중이면 원래 실패 작업에 안내를 보여 주고 버튼은 숨긴다', () => {
  const original = failed([])
  const retry = makeEnvelope({ job: { id: '7002', kind: 'retry', state: 'queued', allowed_actions: ['cancel'] } })

  renderActions(original, { jobs: { [original.job.id]: original, [retry.job.id]: retry } })

  expect(screen.getByText('재시도 대기 중 · 취소하면 건너뛸 수 있습니다')).toBeTruthy()
  expect(screen.queryByRole('button')).toBeNull()
})

test('재시도가 실행 중이면 "재시도 중"으로 보여 준다', () => {
  const original = failed([])
  const retry = makeEnvelope({ job: { id: '7002', kind: 'retry', state: 'running', allowed_actions: [] } })

  renderActions(original, { jobs: { [original.job.id]: original, [retry.job.id]: retry } })

  expect(screen.getByText('재시도 중')).toBeTruthy()
})

test('요청이 실패하면 이유를 알리고 다시 누를 수 있다', async () => {
  const commands = makeCommands({ retry: vi.fn().mockRejectedValue(new Error('다른 재시도가 대기 중입니다.')) })
  renderActions(failed(['retry', 'skip']), { commands })

  fireEvent.click(screen.getByRole('button', { name: '재시도' }))

  expect((await screen.findByRole('alert')).textContent).toBe('다른 재시도가 대기 중입니다.')
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(false),
  )
})

test('요청이 진행 중인 동안은 모든 버튼을 막는다', async () => {
  let release: () => void = () => undefined
  const commands = makeCommands({
    retry: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    ),
  })
  renderActions(failed(['retry', 'skip']), { commands })

  fireEvent.click(screen.getByRole('button', { name: '재시도' }))

  expect((screen.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '건너뛰기' }) as HTMLButtonElement).disabled).toBe(true)

  release()
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(false),
  )
})

test.each(['queued', 'paused'] as const)('%s 작업에는 만료까지 남은 시간을 보여 준다', (state) => {
  const expiresAt = new Date(NOW + 4 * 60_000 + 59_000).toISOString()
  const envelope = makeEnvelope({ job: { id: '7001', state, allowed_actions: ['cancel'], expires_at: expiresAt } })

  renderActions(envelope)

  expect(screen.getByText('만료까지 4분 59초')).toBeTruthy()
})

test('실행 중이거나 끝난 작업에는 만료 시간을 보여 주지 않는다', () => {
  renderActions(failed(['retry', 'skip']))

  expect(screen.queryByText(/만료까지/)).toBeNull()
})

test('버튼이 없는 대기 작업도 만료까지 남은 시간은 보여 준다', () => {
  const envelope = makeEnvelope({
    job: { id: '7001', state: 'queued', allowed_actions: [], expires_at: new Date(NOW + 30_000).toISOString() },
  })

  renderActions(envelope)

  expect(screen.getByText('만료까지 30초')).toBeTruthy()
})
