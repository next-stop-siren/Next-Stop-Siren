/**
 * 개발·테스트용 가짜 대기열 서버. 메모리에서만 동작하며 실제 서버·AI·한도를 쓰지 않는다.
 * 구독 규칙은 docs/04-reference/chat-queue-api-draft.md(PR #56)를 따른다.
 *
 * - 연결을 끊어도 작업은 계속된다. 작업은 서버에 있고 구독은 그 상태를 보는 창일 뿐이다.
 * - delta는 저장하지 않는다. 늦게 연결한 구독자는 놓친 조각을 받지 못한다.
 * - 새 구독은 snapshot으로 시작한다. `since`가 있으면 그 뒤에 끝났거나 버튼이 바뀐 작업도 담는다.
 * - 작업 진행(시작·완료·실패)은 테스트와 미리보기가 직접 호출한다. 시간으로 자동 진행하지 않는다.
 *
 * 재시도·건너뛰기·일시 정지(#21)
 * - 같은 대화의 앞 질문이 실패·중단하면 뒤 질문은 `paused`가 되고, 앞 질문의 재시도가 성공하거나 건너뛰면 다시 `queued`가 된다.
 * - `allowed_actions`는 문서의 버튼 표대로 서버가 정한다. 바뀌면 영향받는 모든 작업의 `job` 이벤트를 함께 보낸다.
 * - 건너뛰기는 그 시점의 실패 하나에 대한 결정이다. 재시도가 다시 실패하면 뒤 질문을 다시 정지시키고 건너뛰기도 다시 허용한다.
 * - 같은 대화의 재시도는 그 대화의 뒤 질문보다 먼저 실행한다(docs/04-reference/chat-policy.md). 다른 대화의 작업은 앞지르지 않는다.
 * - `retry`는 새 작업(kind: retry)을 만든다. 같은 `retry_key`로 다시 보내면 새로 예약하지 않고 기존 작업을 돌려준다.
 * - 시간은 논리 시계다. `advance`로만 흐르고, 10초 간격과 5분 만료는 이 시계를 따른다.
 */

import type { OpenChatEvents } from './chatEventStream'
import { makeJob, makeLimits, makeMessage } from './chatQueueBuilders'
import {
  isTerminalState,
  type ChatEvent,
  type ChatJob,
  type ChatJobAction,
  type ChatJobEnvelope,
  type ChatLimits,
} from './chatQueueTypes'

const BASE_MS = Date.parse('2026-10-20T03:00:00Z')
const WAIT_LIMIT_MS = 5 * 60 * 1000
/** 새 질문·재시도를 접수한 뒤 다음 새 시도까지의 간격 */
const ATTEMPT_INTERVAL_MS = 10 * 1000

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
  /** 질문을 접수한다. 실행은 `start`를 호출해야 시작한다. 같은 대화의 앞 질문이 실패 중이면 `paused`로 접수된다. */
  accept: (conversationId: string, content: string) => ChatJobEnvelope
  start: (jobId: string) => void
  /** 실행 중 작업의 답변 조각을 구독자에게 보낸다. 저장하지 않는다. */
  emitDelta: (jobId: string, text: string) => void
  /** 답변을 저장하고 완료로 알린다. */
  complete: (jobId: string, content?: string) => void
  fail: (jobId: string, errorCode?: string) => void
  interrupt: (jobId: string) => void
  /** 실행 전(`queued`·`paused`) 작업만 취소한다. */
  cancel: (jobId: string) => void
  /** 접수 뒤 5분이 지나 실행 전 작업이 만료된 것으로 처리한다. */
  expire: (jobId: string) => void
  /**
   * 가장 최근 실패 시도를 다시 시도하는 새 작업을 만든다. 서버가 허용하지 않거나(`allowed_actions`),
   * 한도·대기·10초 간격에 걸리면 던진다. 같은 `retryKey`로 다시 보내면 어떤 제한에도 걸리지 않고 기존 작업을 돌려준다.
   */
  retry: (jobId: string, retryKey: string) => ChatJobEnvelope
  /** 실패한 질문을 건너뛰어 뒤 질문을 이어서 처리하게 한다. 서버가 허용하지 않으면 던진다. */
  skip: (jobId: string) => void
  /** 논리 시계를 앞으로 돌린다. */
  advance: (seconds: number) => void
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

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

export function createMockChatServer(): MockChatServer {
  let tick = 0
  let nextJobId = 7000
  let nextMessageId = 9000
  const records = new Map<string, JobRecord>()
  const subscribers = new Set<Subscriber>()
  /** retry_key → 그 키로 만든 재시도 작업과 원래 실패 작업 */
  const retryKeys = new Map<string, { original: string; retry: string }>()
  /**
   * 건너뛴 실패 시도(job.id). 건너뛰기는 그 시점의 실패 하나에 대한 결정이다. 같은 질문의 재시도가 다시 실패하면
   * 새 실패 시도는 이 기록에 없으므로 다시 뒤 질문을 멈춰 세우고 건너뛰기도 다시 허용된다.
   */
  const skippedFailures = new Set<string>()

  const iso = (at = tick) => new Date(BASE_MS + at * 1000).toISOString()
  const nowMs = () => BASE_MS + tick * 1000

  function broadcast(event: ChatEvent) {
    for (const subscriber of [...subscribers]) subscriber.onEvent(event)
  }

  function envelopes(): ChatJobEnvelope[] {
    return [...records.values()].map((record) => record.envelope)
  }

  function attemptsOf(questionMessageId: string): ChatJobEnvelope[] {
    return envelopes().filter((envelope) => envelope.job.question_message_id === questionMessageId)
  }

  function questionJobsOf(conversationId: string): ChatJobEnvelope[] {
    return envelopes().filter(
      (envelope) => envelope.job.kind === 'question' && envelope.job.conversation_id === conversationId,
    )
  }

  const isFailure = (envelope: ChatJobEnvelope) =>
    envelope.job.state === 'failed' || envelope.job.state === 'interrupted'

  /** 이 질문의 가장 최근 실패 시도. 없으면 null */
  function latestFailure(questionMessageId: string): ChatJobEnvelope | null {
    return [...attemptsOf(questionMessageId)].reverse().find(isFailure) ?? null
  }

  /**
   * 실패해서 뒤 질문을 멈춰 세우는 질문인가. 재시도의 실패·취소·만료는 정지를 풀지 않고,
   * 재시도 성공이나 가장 최근 실패를 건너뛰는 것만 정지를 푼다.
   */
  function isStalled(questionMessageId: string): boolean {
    if (attemptsOf(questionMessageId).some((attempt) => attempt.job.state === 'completed')) return false

    const failure = latestFailure(questionMessageId)
    return failure !== null && !skippedFailures.has(failure.job.id)
  }

  /** 같은 대화에서 이 질문이 몇 번째로 접수됐는가 */
  function questionIndex(job: ChatJob): number {
    return questionJobsOf(job.conversation_id).findIndex(
      (envelope) => envelope.job.question_message_id === job.question_message_id,
    )
  }

  /** 이 질문보다 앞선 질문의 재시도가 같은 대화에서 대기 중인가. 재시도가 뒤 질문보다 먼저 실행된다. */
  function hasQueuedRetryAhead(job: ChatJob): boolean {
    const index = questionIndex(job)

    return envelopes().some(
      (envelope) =>
        envelope.job.kind === 'retry' &&
        envelope.job.state === 'queued' &&
        envelope.job.conversation_id === job.conversation_id &&
        questionIndex(envelope.job) < index,
    )
  }

  /** 같은 대화에서 이 질문보다 뒤에 접수된 질문이 이미 실행을 시작했는가 */
  function laterQuestionStarted(job: ChatJob): boolean {
    const list = questionJobsOf(job.conversation_id)
    const index = list.findIndex((envelope) => envelope.job.question_message_id === job.question_message_id)

    return list.slice(index + 1).some((envelope) => envelope.job.started_at !== null)
  }

  /** 이 작업을 멈춰 세우는 앞 질문의 가장 최근 실패 시도. 없으면 null */
  function stallingFailure(job: ChatJob): ChatJobEnvelope | null {
    const list = questionJobsOf(job.conversation_id)
    const index = list.findIndex((envelope) => envelope.job.id === job.id)

    for (const earlier of list.slice(0, Math.max(index, 0))) {
      if (isStalled(earlier.job.question_message_id)) return latestFailure(earlier.job.question_message_id)
    }

    return null
  }

  /** 실패·중단 작업에 허용할 버튼. 한 질문에서 가장 최근 실패 시도에만 버튼이 있다. */
  function failureActions(envelope: ChatJobEnvelope): ChatJobAction[] {
    const { job } = envelope
    const attempts = attemptsOf(job.question_message_id)

    if (attempts.some((attempt) => attempt.job.state === 'completed')) return []
    if (attempts.some((attempt) => attempt.job.state === 'queued' || attempt.job.state === 'running')) return []
    if (latestFailure(job.question_message_id)?.job.id !== job.id) return []
    if (laterQuestionStarted(job)) return []

    return skippedFailures.has(job.id) ? ['retry'] : ['retry', 'skip']
  }

  function applyPatch(id: string, patch: Partial<ChatJob>) {
    const record = records.get(id)
    if (!record) return

    const { job } = record.envelope
    const changed = Object.entries(patch).some(([key, value]) => !sameValue(job[key as keyof ChatJob], value))
    if (changed) records.set(id, { ...record, envelope: { ...record.envelope, job: { ...job, ...patch } } })
  }

  /**
   * 대기 순서. 기본은 먼저 접수한 순서(FIFO)이다. 재시도도 자기가 접수된 자리를 지키고, 다른 대화의 작업이나
   * 이미 실행 중인 작업을 앞지르지 않는다. 다만 같은 대화의 뒤 질문은 그 질문보다 앞선 질문의 재시도 바로 뒤로 밀린다.
   * 예: 같은 대화의 뒤 질문 B, 다른 대화의 질문 C, 앞 질문의 재시도 R 순으로 접수하면 C → R → B.
   */
  function queueOrder(queued: ChatJobEnvelope[]): ChatJobEnvelope[] {
    const sortKey = (envelope: ChatJobEnvelope) => {
      const id = Number(envelope.job.id)
      if (envelope.job.kind !== 'question') return id

      const index = questionIndex(envelope.job)
      const retryIds = queued
        .filter(
          (other) =>
            other.job.kind === 'retry' &&
            other.job.conversation_id === envelope.job.conversation_id &&
            questionIndex(other.job) < index,
        )
        .map((other) => Number(other.job.id))
        .filter((retryId) => retryId > id)

      // 재시도가 더 늦게 접수됐다면 그 재시도 바로 뒤(재시도 번호 + 0.5)로 밀린다.
      return retryIds.length > 0 ? Math.max(...retryIds) + 0.5 : id
    }

    return [...queued].sort((a, b) => sortKey(a) - sortKey(b) || Number(a.job.id) - Number(b.job.id))
  }

  /** 상태에서 정해지는 값(일시 정지 여부, 버튼, 대기 순번)을 다시 계산한다. */
  function derive() {
    for (const envelope of envelopes()) {
      const { job } = envelope

      if (job.state === 'queued' || job.state === 'paused') {
        const blocker = job.kind === 'question' ? stallingFailure(job) : null

        applyPatch(job.id, {
          state: blocker ? 'paused' : 'queued',
          paused_by_message_id: blocker?.answer?.id ?? null,
          allowed_actions: ['cancel'],
        })
      } else if (isFailure(envelope)) {
        applyPatch(job.id, { allowed_actions: failureActions(envelope) })
      }
    }

    const order = queueOrder(envelopes().filter((envelope) => envelope.job.state === 'queued'))

    for (const envelope of envelopes()) {
      const index = order.findIndex((queued) => queued.job.id === envelope.job.id)
      applyPatch(envelope.job.id, { position: index === -1 ? null : index + 1 })
    }
  }

  /**
   * 서버 상태를 바꾸는 한 번의 동작. 바뀐 작업마다 `job` 이벤트를 한 번씩 보내고, 마지막에 `limits`를 보낸다.
   * 한 동작 안에서는 중간 상태를 알리지 않으므로, 구독자는 버튼이 비었다가 다시 생기는 순간을 보지 않는다.
   */
  function mutate(change: () => void) {
    const before = new Map([...records].map(([id, record]) => [id, record.envelope]))

    tick += 1
    change()
    derive()

    for (const [id, record] of records) {
      if (before.get(id) === record.envelope) continue

      record.touched = tick
      broadcast({ type: 'job', data: record.envelope })
    }

    broadcast({ type: 'limits', data: limits() })
  }

  function limits(): ChatLimits {
    const jobs = envelopes().map((envelope) => envelope.job)
    const used = jobs.filter((job) => job.state === 'completed').length
    const reserved = jobs.filter((job) => ['queued', 'paused', 'running'].includes(job.state)).length
    const running = jobs.filter((job) => job.state === 'running').length
    const questionsWaiting = jobs.filter(
      (job) => job.kind === 'question' && (job.state === 'queued' || job.state === 'paused'),
    ).length
    const retriesWaiting = jobs.filter((job) => job.kind === 'retry' && job.state === 'queued').length
    const base = makeLimits()
    const window = (limit: number, resetsAt: string) => ({
      limit,
      used,
      reserved,
      remaining: Math.max(limit - used - reserved, 0),
      resets_at: resetsAt,
    })

    // 직전 새 질문 또는 재시도의 접수 시각 + 10초. 지났으면 null이다.
    const lastAcceptedMs = Math.max(...jobs.map((job) => Date.parse(job.accepted_at)), Number.NEGATIVE_INFINITY)
    const nextAttemptMs = lastAcceptedMs + ATTEMPT_INTERVAL_MS

    return makeLimits({
      daily: window(base.daily.limit, base.daily.resets_at),
      six_hour: window(base.six_hour.limit, base.six_hour.resets_at),
      queue: { ...base.queue, running, questions_waiting: questionsWaiting, retries_waiting: retriesWaiting },
      next_attempt_at: nextAttemptMs > nowMs() ? new Date(nextAttemptMs).toISOString() : null,
    })
  }

  function get(jobId: string): ChatJobEnvelope {
    const record = records.get(jobId)
    if (!record) throw new Error(`없는 작업입니다: ${jobId}`)
    return record.envelope
  }

  function put(envelope: ChatJobEnvelope) {
    const record = records.get(envelope.job.id)
    records.set(envelope.job.id, { envelope, touched: record?.touched ?? tick })
  }

  function newJobEnvelope(job: ChatJob, question: ChatJobEnvelope['question']): ChatJobEnvelope {
    return { job, question, answer: null }
  }

  function finish(
    jobId: string,
    state: Extract<ChatJob['state'], 'completed' | 'failed' | 'interrupted'>,
    answerPatch: { content: string | null; error_code: string | null },
  ) {
    const envelope = get(jobId)
    if (envelope.job.state !== 'running' || !envelope.answer) throw new Error('실행 중인 작업만 끝낼 수 있습니다.')

    mutate(() => {
      const succeeded = state === 'completed'
      const answer = {
        ...envelope.answer!,
        status: state,
        content: answerPatch.content,
        error_code: answerPatch.error_code,
        updated_at: iso(),
        completed_at: succeeded ? iso() : null,
      }

      // 버튼(allowed_actions)은 derive가 문서의 표대로 채운다.
      put({
        ...envelope,
        answer,
        job: { ...envelope.job, state, error_code: answerPatch.error_code, finished_at: iso(), allowed_actions: [] },
      })
    })
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
      const questionId = String(++nextMessageId)

      mutate(() => {
        const question = makeMessage({
          id: questionId,
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
          expires_at: new Date(nowMs() + WAIT_LIMIT_MS).toISOString(),
          allowed_actions: ['cancel'],
        })

        put(newJobEnvelope(job, question))
      })

      return get(jobId)
    },

    start(jobId) {
      const envelope = get(jobId)
      if (envelope.job.state !== 'queued') throw new Error('대기 중인 작업만 시작할 수 있습니다.')
      // 같은 대화의 실패 질문 재시도는 그 대화의 뒤 질문보다 먼저 실행한다(chat-policy.md).
      if (envelope.job.kind === 'question' && hasQueuedRetryAhead(envelope.job)) {
        throw new Error('같은 대화의 앞 질문 재시도가 먼저 실행됩니다.')
      }
      if (envelopes().some((other) => other.job.state === 'running')) {
        throw new Error('실행 중인 작업이 있습니다. 회원당 실행은 1개입니다.')
      }

      mutate(() => {
        // 답변 행은 접수 때가 아니라 실행 시작 때 만든다. 재시도는 같은 질문의 다음 답변이다.
        const answer = makeMessage({
          id: String(++nextMessageId),
          role: 'assistant',
          status: 'pending',
          content: null,
          reply_to_message_id: envelope.question.id,
          attempt_no: attemptsOf(envelope.job.question_message_id).filter((attempt) => attempt.answer).length + 1,
          created_at: iso(),
        })

        put({
          ...envelope,
          answer,
          job: {
            ...envelope.job,
            state: 'running',
            position: null,
            answer_message_id: answer.id,
            started_at: iso(),
            allowed_actions: [],
          },
        })
      })
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
      if (envelope.job.state !== 'queued' && envelope.job.state !== 'paused') {
        throw new Error('실행 전 작업만 취소할 수 있습니다.')
      }

      mutate(() => {
        put({
          ...envelope,
          job: { ...envelope.job, state: 'canceled', position: null, finished_at: iso(), allowed_actions: [] },
        })
      })
    },

    expire(jobId) {
      const envelope = get(jobId)
      if (envelope.job.state !== 'queued' && envelope.job.state !== 'paused') {
        throw new Error('실행 전 작업만 만료될 수 있습니다.')
      }

      mutate(() => {
        put({
          ...envelope,
          job: { ...envelope.job, state: 'expired', position: null, finished_at: iso(), allowed_actions: [] },
        })
      })
    },

    retry(jobId, retryKey) {
      // 응답을 받지 못해 같은 키로 다시 보낸 요청은 새 시도가 아니다. 한도·간격을 보지 않고 기존 결과를 돌려준다.
      const existing = retryKeys.get(retryKey)

      if (existing) {
        if (existing.original !== jobId) throw new Error('같은 retry_key를 다른 작업에 쓸 수 없습니다.')
        return get(existing.retry)
      }

      const original = get(jobId)
      if (!original.job.allowed_actions.includes('retry')) throw new Error('지금은 재시도할 수 없습니다.')

      const current = limits()
      if (current.daily.remaining <= 0 || current.six_hour.remaining <= 0) {
        throw new Error('질문 한도를 모두 썼습니다.')
      }
      if (current.queue.retries_waiting >= current.queue.retries_max) throw new Error('다른 재시도가 대기 중입니다.')
      if (current.next_attempt_at !== null)
        throw new Error('아직 새 시도를 보낼 수 없습니다. 잠시 뒤에 다시 보내 주세요.')

      const retryId = String(++nextJobId)
      retryKeys.set(retryKey, { original: jobId, retry: retryId })

      mutate(() => {
        const job = makeJob({
          id: retryId,
          kind: 'retry',
          conversation_id: original.job.conversation_id,
          question_message_id: original.job.question_message_id,
          state: 'queued',
          position: null,
          accepted_at: iso(),
          expires_at: new Date(nowMs() + WAIT_LIMIT_MS).toISOString(),
          allowed_actions: ['cancel'],
        })

        put(newJobEnvelope(job, original.question))
      })

      return get(retryId)
    },

    skip(jobId) {
      const envelope = get(jobId)
      if (!envelope.job.allowed_actions.includes('skip')) throw new Error('지금은 건너뛸 수 없습니다.')

      mutate(() => {
        skippedFailures.add(envelope.job.id)
      })
    },

    advance(seconds) {
      tick += seconds
    },

    getEnvelope: (jobId) => records.get(jobId)?.envelope ?? null,
    listJobs: () => envelopes(),

    disconnectAll() {
      for (const subscriber of [...subscribers]) subscriber.close()
    },

    subscriberCount: () => subscribers.size,
    serverTime: () => iso(),
  }
}
