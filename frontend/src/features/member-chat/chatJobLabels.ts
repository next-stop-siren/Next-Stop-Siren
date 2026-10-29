import type { ConnectionStatus } from './chatEventsReducer'
import type { ChatJobState } from './memberChatMock'

/** 작업 상태 문구. 기준: docs/04-reference/chat-queue-api-draft.md "상태별 화면 동작" */
export const jobStateLabels: Record<ChatJobState, string> = {
  queued: '대기 중',
  paused: '앞 질문 실패로 대기 중',
  running: '실행 중',
  completed: '완료',
  failed: '실패',
  interrupted: '중단됨',
  canceled: '취소한 질문',
  expired: '대기 시간이 지나 취소됨',
}

export const connectionLabels: Record<ConnectionStatus, string> = {
  idle: '연결 안 됨',
  connecting: '연결 중',
  open: '연결됨',
  reconnecting: '끊김, 다시 연결 중',
}
