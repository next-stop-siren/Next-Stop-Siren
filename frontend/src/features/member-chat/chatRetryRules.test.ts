import { describe, expect, test } from 'vitest'
import { activeRetryFor, formatResetTime, retryBlockReason } from './chatRetryRules'
import { makeEnvelope, makeLimits } from './chatQueueBuilders'
import type { ChatJobEnvelope } from './chatQueueTypes'

const NOW = Date.parse('2026-10-20T03:00:00Z')

function limitsWith(overrides: {
  dailyRemaining?: number
  sixHourRemaining?: number
  retriesWaiting?: number
  questionsWaiting?: number
  nextAttemptAt?: string | null
}) {
  const base = makeLimits()

  return makeLimits({
    daily: { ...base.daily, remaining: overrides.dailyRemaining ?? 400 },
    six_hour: { ...base.six_hour, remaining: overrides.sixHourRemaining ?? 100 },
    queue: {
      ...base.queue,
      retries_waiting: overrides.retriesWaiting ?? 0,
      questions_waiting: overrides.questionsWaiting ?? 0,
    },
    next_attempt_at: overrides.nextAttemptAt ?? null,
  })
}

describe('retryBlockReason', () => {
  test('한도 정보가 없으면 막지 않는다', () => {
    expect(retryBlockReason(null, NOW)).toBeNull()
  })

  test('제한에 걸리지 않으면 null이다', () => {
    expect(retryBlockReason(limitsWith({}), NOW)).toBeNull()
  })

  test('6시간 한도를 다 썼으면 그 초기화 시각을 알려 준다', () => {
    // 6시간 창은 2026-10-20T09:00Z = 한국 시간 오후 6:00에 초기화된다.
    expect(retryBlockReason(limitsWith({ sixHourRemaining: 0 }), NOW)).toBe(
      '질문 한도를 모두 썼습니다. 다음 초기화: 오후 6:00',
    )
  })

  test('일일 한도를 다 썼으면 일일 초기화 시각을 알려 준다', () => {
    // 일일 창은 2026-10-20T15:00Z = 한국 시간 다음 날 오전 12:00이다.
    expect(retryBlockReason(limitsWith({ dailyRemaining: 0 }), NOW)).toBe(
      '질문 한도를 모두 썼습니다. 다음 초기화: 오전 12:00',
    )
  })

  test('둘 다 다 썼으면 더 늦게 풀리는 쪽의 시각을 알려 준다', () => {
    expect(retryBlockReason(limitsWith({ dailyRemaining: 0, sixHourRemaining: 0 }), NOW)).toContain('오전 12:00')
  })

  test('다른 재시도가 대기 중이면 그 이유를 알려 준다', () => {
    expect(retryBlockReason(limitsWith({ retriesWaiting: 1 }), NOW)).toBe('다른 재시도가 대기 중입니다.')
  })

  test('새 질문 대기가 가득 차도 재시도는 막지 않는다', () => {
    expect(retryBlockReason(limitsWith({ questionsWaiting: 3 }), NOW)).toBeNull()
  })

  test('10초 간격이 남았으면 남은 초를 올림해서 알려 준다', () => {
    const nextAttemptAt = new Date(NOW + 2_300).toISOString()

    expect(retryBlockReason(limitsWith({ nextAttemptAt }), NOW)).toBe('3초 뒤에 보낼 수 있습니다')
  })

  test('간격이 지났으면 막지 않는다', () => {
    const nextAttemptAt = new Date(NOW).toISOString()

    expect(retryBlockReason(limitsWith({ nextAttemptAt }), NOW)).toBeNull()
  })

  test('여러 조건이 겹치면 한도, 재시도 대기, 간격 순서로 이유를 보여 준다', () => {
    const nextAttemptAt = new Date(NOW + 5_000).toISOString()

    expect(retryBlockReason(limitsWith({ sixHourRemaining: 0, retriesWaiting: 1, nextAttemptAt }), NOW)).toContain(
      '질문 한도',
    )
    expect(retryBlockReason(limitsWith({ retriesWaiting: 1, nextAttemptAt }), NOW)).toBe('다른 재시도가 대기 중입니다.')
  })
})

test('formatResetTime은 한국 시간 기준으로 쓴다', () => {
  expect(formatResetTime('2026-10-20T09:00:00Z')).toBe('오후 6:00')
})

describe('activeRetryFor', () => {
  function attempt(id: string, kind: 'question' | 'retry', state: ChatJobEnvelope['job']['state']) {
    return makeEnvelope({ job: { id, kind, state, question_message_id: '9001', allowed_actions: [] } })
  }

  const byId = (...envelopes: ChatJobEnvelope[]) => Object.fromEntries(envelopes.map((item) => [item.job.id, item]))

  test.each(['queued', 'running'] as const)('실패 뒤에 %s 재시도가 있으면 그 재시도를 돌려준다', (state) => {
    const failed = attempt('7001', 'question', 'failed')
    const retry = attempt('7002', 'retry', state)

    expect(activeRetryFor(failed.job, byId(failed, retry))?.id).toBe('7002')
  })

  test('진행 중인 재시도가 없으면 null이다', () => {
    const failed = attempt('7001', 'question', 'failed')
    const canceled = attempt('7002', 'retry', 'canceled')

    expect(activeRetryFor(failed.job, byId(failed, canceled))).toBeNull()
  })

  test('더 오래된 실패 시도에는 안내를 달지 않는다', () => {
    const first = attempt('7001', 'question', 'failed')
    const secondFailure = attempt('7002', 'retry', 'failed')
    const active = attempt('7003', 'retry', 'queued')
    const jobs = byId(first, secondFailure, active)

    expect(activeRetryFor(first.job, jobs)).toBeNull()
    expect(activeRetryFor(secondFailure.job, jobs)?.id).toBe('7003')
  })

  test('다른 질문의 재시도는 보지 않는다', () => {
    const failed = attempt('7001', 'question', 'failed')
    const other = makeEnvelope({
      job: { id: '7002', kind: 'retry', state: 'queued', question_message_id: '9999', allowed_actions: [] },
    })

    expect(activeRetryFor(failed.job, byId(failed, other))).toBeNull()
  })

  test('실패하지 않은 작업에는 안내를 달지 않는다', () => {
    const running = attempt('7001', 'question', 'running')
    const retry = attempt('7002', 'retry', 'queued')

    expect(activeRetryFor(running.job, byId(running, retry))).toBeNull()
  })
})
