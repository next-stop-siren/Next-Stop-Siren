/**
 * 구독 이벤트를 화면 상태로 모으는 reducer와 답변 표시 selector.
 * 기준: docs/04-reference/api.md Rev 1 "구독"
 *
 * 핵심 규칙
 * - delta는 임시 출력이다. 저장이 확인된 `completed` 작업의 answer만 확정 답변으로 표시한다.
 * - 실패·중단 작업의 임시 출력은 지우고 다시 보여 주지 않는다.
 * - 종결 상태는 다시 바뀌지 않는다. 늦게 온 이벤트로 되돌리지 않는다.
 * - 같은 작업이 snapshot과 job 이벤트에 겹쳐 와도 `job.id`로 덮어쓰면 된다.
 * - 중간에 연결하면 놓친 delta는 다시 오지 않는다. 그 작업은 완료 이벤트가 올 때까지 "생성 중"으로만 표시한다.
 */

import { isTerminalState, type ChatEvent, type ChatJobEnvelope, type ChatLimits } from './chatQueueTypes'

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting'

export interface ChatEventsState {
  /** job.id → 작업·질문·답변 묶음 */
  jobs: Record<string, ChatJobEnvelope>
  /** job.id → 지금까지 받은 임시 출력. 확정 답변이 아니다. */
  drafts: Record<string, string>
  /** snapshot을 받을 때 이미 실행 중이던 작업. 앞부분 delta를 놓쳤으므로 임시 출력을 보여 주지 않는다. */
  gapped: string[]
  limits: ChatLimits | null
  /** 마지막 snapshot의 server_time. 재연결할 때 `since`로 보낸다. */
  serverTime: string | null
  /** 최종 결과를 `GET /api/chat/jobs/{id}`로 조회해야 하는 작업: snapshot에서 빠진 진행 중 작업, 답변이 비어 온 완료 작업 */
  unresolved: string[]
  connection: ConnectionStatus
}

export const initialChatEventsState: ChatEventsState = {
  jobs: {},
  drafts: {},
  gapped: [],
  limits: null,
  serverTime: null,
  unresolved: [],
  connection: 'idle',
}

export type ChatEventsAction =
  | { type: 'event'; event: ChatEvent }
  | { type: 'connection'; status: ConnectionStatus }
  | { type: 'reset' }

function without<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : list
}

function omit(record: Record<string, string>, key: string): Record<string, string> {
  if (!(key in record)) return record
  const { [key]: _removed, ...rest } = record
  return rest
}

/** 저장이 확인된 최종 답변이 들어 있는가 */
function hasConfirmedAnswer(envelope: ChatJobEnvelope): boolean {
  return envelope.answer?.status === 'completed' && typeof envelope.answer.content === 'string'
}

/**
 * 최종 결과를 `GET /api/chat/jobs/{id}`로 따로 조회해야 하는 작업인가.
 * 목록에서 사라졌거나 아직 종결되지 않았거나, 완료라고 했는데 저장된 답변 내용이 없는 경우다.
 */
function needsLookup(envelope: ChatJobEnvelope | undefined): boolean {
  if (!envelope) return true
  if (!isTerminalState(envelope.job.state)) return true
  return envelope.job.state === 'completed' && !hasConfirmedAnswer(envelope)
}

/** 작업 하나를 반영한다. 이미 종결된 작업의 상태를 다른 상태로 되돌리는 이벤트는 버린다. */
function applyEnvelope(state: ChatEventsState, envelope: ChatJobEnvelope): ChatEventsState {
  const id = envelope.job.id
  const existing = state.jobs[id]

  if (existing && isTerminalState(existing.job.state) && existing.job.state !== envelope.job.state) {
    return state
  }

  const terminal = isTerminalState(envelope.job.state)
  // 완료라고 알렸는데 답변 내용이 비어 있으면 저장된 답변을 다시 조회한다.
  const answerMissing = envelope.job.state === 'completed' && !hasConfirmedAnswer(envelope)

  return {
    ...state,
    jobs: { ...state.jobs, [id]: envelope },
    drafts: terminal ? omit(state.drafts, id) : state.drafts,
    gapped: terminal ? without(state.gapped, id) : state.gapped,
    unresolved: answerMissing
      ? state.unresolved.includes(id)
        ? state.unresolved
        : [...state.unresolved, id]
      : without(state.unresolved, id),
  }
}

function applySnapshot(state: ChatEventsState, event: Extract<ChatEvent, { type: 'snapshot' }>): ChatEventsState {
  const { items, limits, server_time } = event.data
  const inSnapshot = new Set(items.map((item) => item.job.id))

  // 연결이 끊긴 사이 끝나 snapshot에서 빠진 작업은 최종 결과를 따로 조회해야 한다.
  const missing = Object.values(state.jobs)
    .filter((envelope) => !isTerminalState(envelope.job.state) && !inSnapshot.has(envelope.job.id))
    .map((envelope) => envelope.job.id)

  // 끊긴 사이 받지 못한 delta가 있을 수 있어 이전 임시 출력은 모두 버린다.
  let next: ChatEventsState = { ...state, drafts: {}, gapped: [], limits, serverTime: server_time }

  for (const item of items) {
    next = applyEnvelope(next, item)
  }

  // snapshot 시점에 실행 중인 작업은 앞부분을 놓쳤으므로 완료 이벤트를 기다린다.
  const gapped = items.filter((item) => next.jobs[item.job.id]?.job.state === 'running').map((item) => item.job.id)

  return {
    ...next,
    gapped,
    unresolved: [...new Set([...next.unresolved, ...missing])].filter((id) => needsLookup(next.jobs[id])),
  }
}

export function chatEventsReducer(state: ChatEventsState, action: ChatEventsAction): ChatEventsState {
  switch (action.type) {
    case 'reset':
      return initialChatEventsState

    case 'connection':
      return state.connection === action.status ? state : { ...state, connection: action.status }

    case 'event': {
      const { event } = action

      switch (event.type) {
        case 'snapshot':
          return applySnapshot(state, event)

        case 'job':
          return applyEnvelope(state, event.data)

        case 'limits':
          return { ...state, limits: event.data }

        case 'delta': {
          const { job_id, text } = event.data
          const job = state.jobs[job_id]

          // 실행 중으로 알고 있고 앞부분을 놓치지 않은 작업의 조각만 이어 붙인다.
          if (job?.job.state !== 'running' || state.gapped.includes(job_id)) return state

          return { ...state, drafts: { ...state.drafts, [job_id]: (state.drafts[job_id] ?? '') + text } }
        }
      }
    }
  }
}

export type AnswerView =
  /** 실행 전이거나 취소·만료. 보여 줄 답변이 없다. */
  | { kind: 'none' }
  /** 실행 중이지만 보여 줄 임시 출력이 없다. */
  | { kind: 'generating' }
  /** 실행 중에 받은 임시 출력. DB에 저장되지 않았다. */
  | { kind: 'draft'; text: string }
  /** 저장이 확인된 최종 답변 */
  | { kind: 'confirmed'; content: string }
  /** 완료로 알렸지만 저장된 답변 내용을 아직 받지 못했다. */
  | { kind: 'unconfirmed' }
  /** 실패·중단. 받은 임시 출력은 보여 주지 않는다. */
  | { kind: 'failed'; state: 'failed' | 'interrupted'; errorCode: string | null }

export function selectAnswerView(state: ChatEventsState, jobId: string): AnswerView {
  const envelope = state.jobs[jobId]
  if (!envelope) return { kind: 'none' }

  const { job, answer } = envelope

  switch (job.state) {
    case 'completed':
      return answer?.status === 'completed' && typeof answer.content === 'string'
        ? { kind: 'confirmed', content: answer.content }
        : { kind: 'unconfirmed' }

    case 'failed':
    case 'interrupted':
      return { kind: 'failed', state: job.state, errorCode: job.error_code ?? answer?.error_code ?? null }

    case 'running': {
      const text = state.drafts[jobId]
      return text ? { kind: 'draft', text } : { kind: 'generating' }
    }

    default:
      return { kind: 'none' }
  }
}
