import { useState } from 'react'
import { activeRetryFor, formatExpiresIn, retryBlockReason } from './chatRetryRules'
import type { ChatJobEnvelope, ChatLimits } from './chatQueueTypes'
import type { ChatJobCommands } from './useChatJobCommands'
import styles from './ChatJobActions.module.css'

type ChatJobActionsProps = {
  envelope: ChatJobEnvelope
  /** 같은 질문의 다른 시도(재시도)를 찾기 위한 전체 작업 */
  jobs: Record<string, ChatJobEnvelope>
  limits: ChatLimits | null
  /** 서버 시각에 맞춘 지금 시각(ms). 남은 초를 계산하는 데 쓴다. 호출한 쪽이 시간이 흐르면 다시 그려야 한다. */
  nowMs: number
  commands: ChatJobCommands
}

/**
 * 작업 하나의 재시도·건너뛰기·취소 버튼과 안내 문구.
 * 버튼은 서버가 준 `allowed_actions`에 있을 때만 보이고, 한도·간격 때문에 막힌 재시도는 이유와 함께 비활성화한다.
 * 버튼을 눌러도 화면은 바로 바뀌지 않는다. 서버가 보내는 job 이벤트로 바뀐다.
 */
function ChatJobActions({ envelope, jobs, limits, nowMs, commands }: ChatJobActionsProps) {
  const { job } = envelope
  const [pending, setPending] = useState<string | null>(null)
  const [error, setError] = useState('')

  const actions = job.allowed_actions
  const activeRetry = activeRetryFor(job, jobs)
  const retryBlocked = actions.includes('retry') ? retryBlockReason(limits, nowMs) : null

  const run = async (name: string, send: (jobId: string) => Promise<void>) => {
    if (pending) return

    setPending(name)
    setError('')

    try {
      await send(job.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '요청을 보내지 못했습니다.')
    } finally {
      setPending(null)
    }
  }

  const reasonId = `retry-reason-${job.id}`
  const waiting = job.state === 'queued' || job.state === 'paused'

  if (actions.length === 0 && !activeRetry && !waiting && !error) return null

  return (
    <div className={styles.actions}>
      {waiting && <p className={styles.note}>만료까지 {formatExpiresIn(job.expires_at, nowMs)}</p>}

      {activeRetry && (
        <p className={styles.note} role="status">
          {activeRetry.state === 'running' ? '재시도 중' : '재시도 대기 중 · 취소하면 건너뛸 수 있습니다'}
        </p>
      )}

      {actions.length > 0 && (
        <div className={styles.buttons}>
          {actions.includes('retry') && (
            <button
              type="button"
              disabled={pending !== null || retryBlocked !== null}
              aria-describedby={retryBlocked ? reasonId : undefined}
              onClick={() => void run('retry', commands.retry)}
            >
              재시도
            </button>
          )}
          {actions.includes('skip') && (
            <button type="button" disabled={pending !== null} onClick={() => void run('skip', commands.skip)}>
              건너뛰기
            </button>
          )}
          {actions.includes('cancel') && (
            <button type="button" disabled={pending !== null} onClick={() => void run('cancel', commands.cancel)}>
              {job.kind === 'retry' ? '재시도 취소' : '질문 취소'}
            </button>
          )}
        </div>
      )}

      {retryBlocked && (
        <p id={reasonId} className={styles.reason}>
          {retryBlocked}
        </p>
      )}

      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

export default ChatJobActions
