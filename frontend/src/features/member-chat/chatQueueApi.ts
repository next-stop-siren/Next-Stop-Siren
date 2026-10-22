/**
 * 대기열 API 호출 (제안 초안, #53 확정 전). 기준: docs/04-reference/chat-queue-api-draft.md
 * 구독(`GET /api/chat/events`)은 chatEventStream.ts에 있다.
 */

import { isEnvelope } from './chatEventStream'
import type { ChatJobEnvelope } from './chatQueueTypes'

/**
 * 작업 하나를 조회한다 — `GET /api/chat/jobs/{id}`.
 * 화면이 진행 중으로 알던 작업이 재연결 snapshot에 없을 때 최종 결과를 확인하는 데 쓴다.
 * 없는 작업과 타인의 작업은 같은 404라 null을 돌려준다.
 */
export async function fetchChatJob(accessToken: string, jobId: string): Promise<ChatJobEnvelope | null> {
  const response = await fetch(`/api/chat/jobs/${encodeURIComponent(jobId)}`, {
    headers: { Accept: 'application/json', Authorization: `Bearer ${accessToken}` },
  })

  if (response.status === 404) return null
  if (!response.ok) throw new Error(`작업을 조회하지 못했습니다 (${response.status}).`)

  const body: unknown = await response.json().catch(() => null)
  if (!isEnvelope(body)) throw new Error('작업 조회 응답이 계약과 다릅니다.')

  return body
}
