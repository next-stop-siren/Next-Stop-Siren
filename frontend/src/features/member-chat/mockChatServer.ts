/**
 * 개발·테스트용 가짜 대기열 서버. 메모리에서만 동작하며 실제 서버·AI·한도를 쓰지 않는다.
 * 구독 규칙은 docs/04-reference/chat-queue-api-draft.md(PR #56)를 따른다.
 *
 * - 연결을 끊어도 작업은 계속된다. 작업은 서버에 있고 구독은 그 상태를 보는 창일 뿐이다.
 * - delta는 저장하지 않는다. 늦게 연결한 구독자는 놓친 조각을 받지 못한다.
 * - 새 구독은 snapshot으로 시작한다. `since`가 있으면 그 뒤에 끝났거나 버튼이 바뀐 작업도 담는다.
 * - 작업 진행(시작·완료·실패)은 테스트와 미리보기가 직접 호출한다. 시간으로 자동 진행하지 않는다.
 *
 * 재시도·건너뛰기·일시 정지는 #21 범위라 구현하지 않는다.
 */

import type { OpenChatEvents } from './chatEventStream'
import { makeJob, makeLimits, makeMessage } from './chatQueueBuilders'
import { isTerminalState, type ChatEvent, type ChatJob, type ChatJobEnvelope, type ChatLimits } from './chatQueueTypes'

const BASE_MS = Date.parse('2026-10-20T03:00:00Z')
const WAIT_LIMIT_MS = 5 * 60 * 1000

interface JobRecord {
  envelope: ChatJobEnvelope
  /** 상태나 allowed_actions가 마지막으로 바뀐 서버 시각(논리 시계) */
  touched: number
}

interface Subscriber {
  onEvent: (event: ChatEvent) => void
  close: () => void
}

export interface MockChatServer {
  /** 실제 `openChatEvents`와 같은 모양이다. 서버가 연결을 닫으면 resolve, 중단하면 AbortError로 reject한다. */
  open: OpenChatEvents
  /** 질문을 접수한다. 실행은 `start`를 호출해야 시작한다. */
  accept: (conversationId: string, content: string) => ChatJobEnvelope
  start: (jobId: string) => void
  /** 실행 중 작업의 답변 조각을 구독자에게 보낸다. 저장하지 않는다. */
  emitDelta: (jobId: string, text: string) => void
  /** 답변을 저장하고 완료로 알린다. */
  complete: (jobId: string, content?: string) => void
  fail: (jobId: string, errorCode?: string) => void
  interrupt: (jobId: string) => void
  /** 실행 전 작업만 취소한다. */
  cancel: (jobId: string) => void
  /** `GET /api/chat/jobs/{id}`에 해당한다. */
  getEnvelope: (jobId: string) => ChatJobEnvelope | null
  listJobs: () => ChatJobEnvelope[]
  /** 모든 구독 연결을 서버 쪽에서 끊는다(탭 닫기·네트워크 끊김). 작업은 계속된다. */
  disconnectAll: () => void
  subscriberCount: () => number
  /** 지금 서버 시각. snapshot의 server_time과 같은 형식이다. */
  serverTime: () => string
}

export const MOCK_ANSWER_TEXT = '목업 서버가 저장한 답변입니다. 실제 AI 응답이 아닙니다.'

export function createMockChatServer(): MockChatServer {
  let tick = 0
  let nextJobId = 7000
  let nextMessageId = 9000
  const records = new Map<string, JobRecord>()
  const subscribers = new Set<Subscriber>()

  const iso = (at = tick) => new Date(BASE_MS + at * 1000).toISOString()

  function broadcast(event: ChatEvent) {
    for (const subscriber of [...subscribers]) subscriber.onEvent(event)
  }

  function limits(): ChatLimits {
    const jobs = [...records.values()].map((record) => record.envelope.job)
    const used = jobs.filter((job) => job.state === 'completed').length
    const queued = jobs.filter((job) => job.state === 'queued').length
    const running = jobs.filter((job) => job.state === 'running').length
    const reserved = queued + running
    const base = makeLimits()
    const window = (limit: number, resetsAt: string) => ({
      limit,
      used,
      reserved,
      remaining: Math.max(limit - used - reserved, 0),
      resets_at: resetsAt,
    })

    return makeLimits({
      daily: window(base.daily.limit, base.daily.resets_at),
      six_hour: window(base.six_hour.limit, base.six_hour.resets_at),
      queue: { ...base.queue, running, questions_waiting: queued },
    })
  }

  function setEnvelope(envelope: ChatJobEnvelope) {
    tick += 1
    records.set(envelope.job.id, { envelope, touched: tick })
    broadcast({ type: 'job', data: envelope })
  }

  /** 대기 순번을 접수 순서대로 다시 매긴다. 순번이 바뀐 작업은 job 이벤트를 보낸다. */
  function renumber() {
    let position = 0

    for (const record of [...records.values()]) {
      const { job } = record.envelope
      const next = job.state === 'queued' ? ++position : null

      if (job.position !== next) setEnvelope({ ...record.envelope, job: { ...job, position: next } })
    }
  }

  function get(jobId: string): ChatJobEnvelope {
    const record = records.get(jobId)
    if (!record) throw new Error(`없는 작업입니다: ${jobId}`)
    return record.envelope
  }

  function finish(
    jobId: string,
    state: Extract<ChatJob['state'], 'completed' | 'failed' | 'interrupted'>,
    answerPatch: { content: string | null; error_code: string | null },
  ) {
    const envelope = get(jobId)
    if (envelope.job.state !== 'running' || !envelope.answer) throw new Error('실행 중인 작업만 끝낼 수 있습니다.')

    const succeeded = state === 'completed'
    const answer = {
      ...envelope.answer,
      status: state,
      content: answerPatch.content,
      error_code: answerPatch.error_code,
      updated_at: iso(tick + 1),
      completed_at: succeeded ? iso(tick + 1) : null,
    }

    setEnvelope({
      ...envelope,
      answer,
      job: {
        ...envelope.job,
        state,
        error_code: answerPatch.error_code,
        finished_at: iso(tick + 1),
        // 실패·중단 직후에는 재시도·건너뛰기가 허용된다(재시도 동작 자체는 #21).
        allowed_actions: succeeded ? [] : ['retry', 'skip'],
      },
    })
    broadcast({ type: 'limits', data: limits() })
  }

  function snapshotFor(since: string | null | undefined): ChatEvent {
    const sinceTick = since ? Math.floor((Date.parse(since) - BASE_MS) / 1000) : Number.NaN
    const hasSince = !Number.isNaN(sinceTick)

    // 종결 전 작업은 항상, 종결된 작업은 since 뒤에 끝났거나 버튼이 바뀐 것만 담는다.
    const items = [...records.values()]
      .filter((record) => !isTerminalState(record.envelope.job.state) || (hasSince && record.touched > sinceTick))
      .map((record) => record.envelope)

    return { type: 'snapshot', data: { items, limits: limits(), server_time: iso() } }
  }

  const open: MockChatServer['open'] = ({ since, signal, onEvent }) =>
    new Promise<void>((resolve, reject) => {
      if (signal.aborted) {
        reject(new DOMException('Aborted', 'AbortError'))
        return
      }

      const onAbort = () => {
        subscribers.delete(subscriber)
        reject(new DOMException('Aborted', 'AbortError'))
      }

      const subscriber: Subscriber = {
        onEvent,
        close: () => {
          subscribers.delete(subscriber)
          signal.removeEventListener('abort', onAbort)
          resolve()
        },
      }

      subscribers.add(subscriber)
      signal.addEventListener('abort', onAbort, { once: true })
      onEvent(snapshotFor(since))
    })

  return {
    open,

    accept(conversationId, content) {
      const jobId = String(++nextJobId)
      const question = makeMessage({
        id: String(++nextMessageId),
        role: 'user',
        status: 'completed',
        content,
        request_key: `mock-request-${jobId}`,
        created_at: iso(),
      })
      const job = makeJob({
        id: jobId,
        conversation_id: conversationId,
        question_message_id: question.id,
        state: 'queued',
        position: null,
        accepted_at: iso(),
        expires_at: new Date(BASE_MS + tick * 1000 + WAIT_LIMIT_MS).toISOString(),
        allowed_actions: ['cancel'],
      })

      setEnvelope({ job, question, answer: null })
      renumber()
      broadcast({ type: 'limits', data: limits() })

      return get(jobId)
    },

    start(jobId) {
      const envelope = get(jobId)
      if (envelope.job.state !== 'queued') throw new Error('대기 중인 작업만 시작할 수 있습니다.')
      if ([...records.values()].some((record) => record.envelope.job.state === 'running')) {
        throw new Error('실행 중인 작업이 있습니다. 회원당 실행은 1개입니다.')
      }

      // 답변 행은 접수 때가 아니라 실행 시작 때 만든다.
      const answer = makeMessage({
        id: String(++nextMessageId),
        role: 'assistant',
        status: 'pending',
        content: null,
        reply_to_message_id: envelope.question.id,
        attempt_no: 1,
        created_at: iso(tick + 1),
      })

      setEnvelope({
        ...envelope,
        answer,
        job: {
          ...envelope.job,
          state: 'running',
          position: null,
          answer_message_id: answer.id,
          started_at: iso(tick + 1),
          allowed_actions: [],
        },
      })
      renumber()
      broadcast({ type: 'limits', data: limits() })
    },

    emitDelta(jobId, text) {
      if (get(jobId).job.state !== 'running') throw new Error('실행 중인 작업만 조각을 보낼 수 있습니다.')
      broadcast({ type: 'delta', data: { job_id: jobId, text } })
    },

    complete(jobId, content = MOCK_ANSWER_TEXT) {
      finish(jobId, 'completed', { content, error_code: null })
    },

    fail(jobId, errorCode = 'provider_failed') {
      finish(jobId, 'failed', { content: null, error_code: errorCode })
    },

    interrupt(jobId) {
      finish(jobId, 'interrupted', { content: null, error_code: 'outcome_unknown' })
    },

    cancel(jobId) {
      const envelope = get(jobId)
      if (envelope.job.state !== 'queued') throw new Error('실행 전 작업만 취소할 수 있습니다.')

      setEnvelope({
        ...envelope,
        job: { ...envelope.job, state: 'canceled', position: null, finished_at: iso(tick + 1), allowed_actions: [] },
      })
      renumber()
      broadcast({ type: 'limits', data: limits() })
    },

    getEnvelope: (jobId) => records.get(jobId)?.envelope ?? null,
    listJobs: () => [...records.values()].map((record) => record.envelope),

    disconnectAll() {
      for (const subscriber of [...subscribers]) subscriber.close()
    },

    subscriberCount: () => subscribers.size,
    serverTime: () => iso(),
  }
}
