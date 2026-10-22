import type { AnswerView } from './chatEventsReducer'
import styles from './ChatAnswerView.module.css'

type ChatAnswerViewProps = {
  view: AnswerView
}

/**
 * 작업 하나의 답변 표시. 스트리밍 중 받은 조각은 "임시 출력"으로, DB 저장이 확인된 결과만 "확정 답변"으로 보여
 * 둘을 헷갈리지 않게 한다. 실패·중단한 작업의 임시 출력은 보여 주지 않는다.
 */
function ChatAnswerView({ view }: ChatAnswerViewProps) {
  switch (view.kind) {
    case 'none':
      return null

    case 'generating':
      return (
        <p className={styles.status} role="status">
          답변 생성 중입니다.
        </p>
      )

    case 'draft':
      return (
        <div className={styles.draft}>
          <p className={styles.label}>생성 중인 임시 출력 · 아직 저장되지 않았습니다</p>
          <p className={styles.text}>{view.text}</p>
        </div>
      )

    case 'confirmed':
      return (
        <div className={styles.confirmed}>
          <p className={styles.label}>답변 완료 · 저장됨</p>
          <p className={styles.text}>{view.content}</p>
        </div>
      )

    case 'unconfirmed':
      return (
        <p className={styles.status} role="status">
          저장된 답변을 확인하는 중입니다.
        </p>
      )

    case 'failed':
      return (
        <p className={styles.failed} role="status">
          {view.state === 'interrupted' ? '답변 생성이 중단되었습니다.' : '답변을 만들지 못했습니다.'}
          {view.errorCode ? ` (오류 코드: ${view.errorCode})` : ''}
        </p>
      )
  }
}

export default ChatAnswerView
