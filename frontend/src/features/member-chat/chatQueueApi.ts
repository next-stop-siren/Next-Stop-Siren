/**
 * 대기열 API 호출. 기준: docs/04-reference/api.md Rev 1 "회원 채팅 대기열 (#53)"
 * 구독(`GET /api/chat/events`)은 chatEventStream.ts에, 질문 접수·재시도·건너뛰기는 memberChatApi.ts에 있다.
 */

import { isEnvelope } from './chatEventStream'
import type { ChatCancelResult, ChatJobEnvelope, ChatJobPage } from './chatQueueTypes'
import { ChatApiError, requestJson } from './memberChatApi'

/**
 * 작업 하나를 조회한다 — `GET /api/chat/jobs/{id}`.
 * 화면이 진행 중으로 알던 작업이 재연결 snapshot에 없을 때 최종 결과를 확인하는 데 쓴다.
 * 없는 작업과 타인의 작업은 같은 404라 null을 돌려준다.
 */
export async function fetchChatJob(
  accessToken: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<ChatJobEnvelope | null> {
  let body: unknown

  try {
    body = await requestJson<unknown>(`/api/chat/jobs/${encodeURIComponent(jobId)}`, accessToken, { signal })
  } catch (error) {
    if (error instanceof ChatApiError && error.status === 404) return null
    throw error
  }

  if (!isEnvelope(body)) throw new Error('작업 조회 응답이 계약과 다릅니다.')

  return body
}

/**
 * 한 대화의 작업을 최신순으로 읽는다 — `GET /api/chat/jobs?conversation_id=`.
 * 재연결 snapshot에는 끝나지 않은 작업만 오므로, 새로고침 뒤 실패한 질문의 재시도·건너뛰기 버튼은 이 목록으로 되살린다.
 */
export async function listConversationJobs(
  accessToken: string,
  conversationId: string,
  limit = 50,
  signal?: AbortSignal,
): Promise<ChatJobPage> {
  const params = new URLSearchParams({ conversation_id: conversationId, limit: String(limit) })
  return requestJson(`/api/chat/jobs?${params.toString()}`, accessToken, { signal })
}

/** 실행 전(`queued`·`paused`) 작업을 취소한다. 실행 중인 작업은 취소할 수 없다. */
export async function cancelChatJob(
  accessToken: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<ChatCancelResult> {
  return requestJson(`/api/chat/jobs/${encodeURIComponent(jobId)}/cancel`, accessToken, {
    method: 'POST',
    body: JSON.stringify({}),
    signal,
  })
}
