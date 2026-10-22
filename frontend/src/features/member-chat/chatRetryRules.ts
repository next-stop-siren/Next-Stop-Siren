/**
 * 재시도·건너뛰기 버튼의 표시 규칙. 기준: docs/04-reference/chat-queue-api-draft.md (PR #56)
 *
 * 버튼을 보일지는 서버가 준 `allowed_actions`만으로 정한다. 여기서는 허용된 재시도 버튼을
 * `limits` 때문에 잠시 막아야 하는지와 그 이유 문구만 계산한다. 잔량을 화면에서 다시 계산하지 않는다.
 */

import type { ChatJob, ChatJobEnvelope, ChatLimits } from './chatQueueTypes'

/** 초기화 시각을 한국 시간 "오후 6:00" 모양으로 쓴다. 서버는 KST 기준으로 초기화한다. */
export function formatResetTime(resetsAt: string): string {
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(new Date(resetsAt))
}

/**
 * 허용된 재시도 버튼을 비활성화해야 하면 그 이유를, 아니면 null을 돌려준다.
 * 새 질문 대기(`questions_waiting`)가 가득 차도 재시도는 막지 않는다. 입력창만 막는 조건이기 때문이다.
 *
 * @param nowMs 지금 시각(ms). 서버 시각에 맞춘 값을 넘긴다.
 */
export function retryBlockReason(limits: ChatLimits | null, nowMs: number): string | null {
  if (!limits) return null

  const exhausted = [limits.daily, limits.six_hour].filter((window) => window.remaining <= 0)

  if (exhausted.length > 0) {
    // 둘 다 소진됐으면 더 늦게 초기화되는 쪽이 풀려야 다시 보낼 수 있다.
    const resetsAt = exhausted.map((window) => window.resets_at).sort()[exhausted.length - 1]
    return `질문 한도를 모두 썼습니다. 다음 초기화: ${formatResetTime(resetsAt)}`
  }

  if (limits.queue.retries_waiting >= limits.queue.retries_max) return '다른 재시도가 대기 중입니다.'

  if (limits.next_attempt_at) {
    const seconds = Math.ceil((Date.parse(limits.next_attempt_at) - nowMs) / 1000)
    if (seconds > 0) return `${seconds}초 뒤에 보낼 수 있습니다`
  }

  return null
}

/**
 * 실패한 작업 뒤에 이어진 재시도 중 지금 진행 중인(`queued`·`running`) 것.
 * 이 작업이 그 재시도 바로 앞의 실패 시도일 때만 돌려준다. 더 오래된 실패 시도에는 안내를 달지 않는다.
 */
export function activeRetryFor(job: ChatJob, jobs: Record<string, ChatJobEnvelope>): ChatJob | null {
  if (job.state !== 'failed' && job.state !== 'interrupted') return null

  const attempts = Object.values(jobs)
    .map((envelope) => envelope.job)
    .filter((candidate) => candidate.question_message_id === job.question_message_id)
    .sort((a, b) => Number(a.id) - Number(b.id))

  const last = attempts[attempts.length - 1]
  if (last?.kind !== 'retry' || (last.state !== 'queued' && last.state !== 'running')) return null

  const before = attempts.slice(0, -1)
  const latestFailure = [...before]
    .reverse()
    .find((attempt) => attempt.state === 'failed' || attempt.state === 'interrupted')

  return latestFailure?.id === job.id ? last : null
}

/**
 * 실행 전 작업이 만료되기까지 남은 시간을 "4분 59초" 모양으로 쓴다. 기한은 접수 5분 뒤(`expires_at`)다.
 *
 * @param nowMs 지금 시각(ms). 서버 시각에 맞춘 값을 넘긴다.
 */
export function formatExpiresIn(expiresAt: string, nowMs: number): string {
  const seconds = Math.ceil((Date.parse(expiresAt) - nowMs) / 1000)

  if (seconds <= 0) return '곧 만료됩니다'
  if (seconds < 60) return `${seconds}초`

  return `${Math.floor(seconds / 60)}분 ${seconds % 60}초`
}
