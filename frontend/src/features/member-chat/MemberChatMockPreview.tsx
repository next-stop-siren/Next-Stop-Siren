import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  allowedTransitions,
  checkEnqueue,
  createInitialMemberChatState,
  createQuestionMessage,
  expireState,
  hasBlockingFailure,
  QUESTIONS_MAX,
  RETRIES_MAX,
  RUNNING_MAX,
  summarizeLimits,
  summarizeQueue,
  transitionTask,
  isWaitingStatus,
  type ChatJobState,
  type EnqueueRejection,
  type MemberChatMockState,
} from './memberChatMock'
import styles from './MemberChatMockPreview.module.css'

const statusLabels: Record<ChatJobState, string> = {
  queued: '대기 중',
  running: '실행 중',
  paused: '앞 질문 실패로 대기 중',
  completed: '완료',
  failed: '실패',
  interrupted: '중단됨',
  canceled: '취소한 질문',
  expired: '대기 시간이 지나 취소됨',
}

const actionLabels: Partial<Record<ChatJobState, string>> = {
  queued: '대기로 (재개)',
  running: '실행 시작',
  completed: '완료로',
  failed: '실패로',
  interrupted: '중단으로',
  canceled: '취소',
}

const rejectionMessages: Record<EnqueueRejection, string> = {
  queue_full: `대기 중인 질문이 ${QUESTIONS_MAX}개입니다. 답변 뒤에 보내 주세요.`,
  quota_exceeded: '질문 한도를 모두 썼습니다.',
}

const statusOrder: ChatJobState[] = [
  'queued',
  'running',
  'paused',
  'completed',
  'failed',
  'interrupted',
  'canceled',
  'expired',
]

function createId(): string {
  return `mock-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function formatRemaining(expiresAt: string, nowMs: number): string {
  const seconds = Math.max(Math.ceil((new Date(expiresAt).getTime() - nowMs) / 1000), 0)
  return `${Math.floor(seconds / 60)}분 ${String(seconds % 60).padStart(2, '0')}초`
}

export default function MemberChatMockPreview() {
  const [state, setState] = useState<MemberChatMockState>(() => createInitialMemberChatState())
  const [question, setQuestion] = useState('')
  const [rejection, setRejection] = useState<EnqueueRejection | null>(null)
  const [activeConversationId, setActiveConversationId] = useState(state.activeConversationId)
  const [nowMs, setNowMs] = useState(() => Date.now())

  const activeConversation =
    state.conversations.find((conversation) => conversation.id === activeConversationId) ?? state.conversations[0]
  const messages = activeConversation?.messages ?? []

  useEffect(() => {
    const tick = () => {
      const now = Date.now()
      setNowMs(now)
      setState((current) => expireState(current, now))
    }

    tick()

    const timerId = window.setInterval(tick, 1000)

    return () => window.clearInterval(timerId)
  }, [])

  // 목업 전체 대화의 작업 수와 한도를 집계합니다. 실제 서버 대기열이 아닙니다.
  const queue = useMemo(() => summarizeQueue(state.conversations), [state.conversations])
  const limits = useMemo(() => summarizeLimits(state.conversations), [state.conversations])

  const counts = useMemo(() => {
    const result = Object.fromEntries(statusOrder.map((status) => [status, 0])) as Record<ChatJobState, number>

    for (const message of messages) {
      result[message.status] += 1
    }

    return result
  }, [messages])

  const updateMessage = (messageId: string, nextStatus: ChatJobState) => {
    setState((current) => transitionTask(current, activeConversationId, messageId, nextStatus))
  }

  const addQuestion = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmed = question.trim()
    if (!trimmed || !activeConversation) return

    const rejected = checkEnqueue(state.conversations)

    if (rejected) {
      setRejection(rejected)
      return
    }

    // 같은 대화에 실패·중단된 질문이 있으면 새 질문도 정지 상태로 접수됩니다.
    const newMessage = createQuestionMessage(createId(), trimmed, hasBlockingFailure(activeConversation.messages))

    setState((current) => ({
      ...current,
      conversations: current.conversations.map((conversation) =>
        conversation.id === activeConversationId
          ? { ...conversation, messages: [...conversation.messages, newMessage] }
          : conversation,
      ),
    }))
    setRejection(null)
    setQuestion('')
  }

  const reset = () => {
    const fresh = createInitialMemberChatState()
    setState(fresh)
    setActiveConversationId(fresh.activeConversationId)
    setQuestion('')
    setRejection(null)
  }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>DEVELOPMENT PREVIEW</p>
          <h1>대화 · 작업 상태 목업</h1>
          <p>질문 추가와 상태 전이를 확인하는 로컬 화면입니다.</p>
        </div>
        <span className={styles.badge}>실제 API 미연결</span>
      </header>

      <aside className={styles.notice} role="note">
        이 화면은 브라우저 메모리에서만 동작합니다. 새로고침하면 초기화되며, 실제 대기열·AI 호출·서버 저장·할당량을
        사용하지 않습니다.
      </aside>

      <section className={styles.panel} aria-label="대화 선택">
        <div className={styles.panelHeader}>
          <div>
            <h2>대화 목록</h2>
            <p>확인할 목업 대화를 선택하세요.</p>
          </div>
          <button type="button" className={styles.secondaryButton} onClick={reset}>
            초기화
          </button>
        </div>
        <div className={styles.conversationList}>
          {state.conversations.map((conversation) => (
            <button
              type="button"
              key={conversation.id}
              className={`${styles.conversationButton} ${conversation.id === activeConversationId ? styles.selected : ''}`}
              aria-pressed={conversation.id === activeConversationId}
              onClick={() => setActiveConversationId(conversation.id)}
            >
              <span>{conversation.title}</span>
              <small>{conversation.messages.length}개 질문</small>
            </button>
          ))}
        </div>
        <section className={styles.stats} aria-label="선택한 대화의 작업 상태 집계">
          <div>
            <span>대기 중</span>
            <strong>{counts.queued}</strong>
          </div>
          <div>
            <span>실행 중</span>
            <strong>{counts.running}</strong>
          </div>
          <div>
            <span>완료</span>
            <strong>{counts.completed}</strong>
          </div>
          <div>
            <span>실패</span>
            <strong>{counts.failed}</strong>
          </div>
        </section>

        <section className={styles.stats} aria-label="목업 전체 대기열">
          <div>
            <span>전체 실행 중</span>
            <strong>
              {queue.running} / {RUNNING_MAX}
            </strong>
          </div>
          <div>
            <span>새 질문 대기</span>
            <strong>
              {queue.waitingQuestions} / {QUESTIONS_MAX}
            </strong>
          </div>
          <div>
            <span>재시도 대기</span>
            <strong>
              {queue.waitingRetries} / {RETRIES_MAX}
            </strong>
          </div>
        </section>

        <section className={styles.stats} aria-label="목업 할당량">
          <div>
            <span>일일 잔여량</span>
            <strong>
              {limits.daily.remaining} / {limits.daily.limit}
            </strong>
          </div>
          <div>
            <span>6시간 잔여량</span>
            <strong>
              {limits.sixHour.remaining} / {limits.sixHour.limit}
            </strong>
          </div>
        </section>
      </section>

      <section className={styles.messages} aria-label="질문과 답변">
        <h2>{activeConversation?.title ?? '대화'}</h2>
        {messages.length === 0 ? (
          <div className={styles.empty}>아직 질문이 없습니다. 아래에서 목업 질문을 추가해 보세요.</div>
        ) : (
          messages.map((message) => {
            const transitions = allowedTransitions[message.status]

            return (
              <article className={styles.messageCard} key={message.id}>
                <div className={styles.questionLabel}>질문</div>
                <p className={styles.question}>{message.question}</p>
                <div className={styles.statusRow}>
                  <span className={`${styles.status} ${styles[`status_${message.status}`]}`}>
                    {statusLabels[message.status]}
                  </span>
                  <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>
                  {message.acceptedAt && (
                    <time dateTime={message.acceptedAt}>접수 {new Date(message.acceptedAt).toLocaleString()}</time>
                  )}
                  {message.expiresAt && (
                    <time dateTime={message.expiresAt}>만료 {new Date(message.expiresAt).toLocaleString()}</time>
                  )}
                  {message.expiresAt && isWaitingStatus(message.status) && (
                    <span>만료까지 {formatRemaining(message.expiresAt, nowMs)}</span>
                  )}
                </div>
                {message.answer && (
                  <div className={styles.answer}>
                    <strong>답변</strong>
                    <p>{message.answer}</p>
                  </div>
                )}

                {transitions.length > 0 && (
                  <section className={styles.actions} aria-label="목업 상태 전이">
                    {transitions.map((nextStatus) => (
                      <button type="button" key={nextStatus} onClick={() => updateMessage(message.id, nextStatus)}>
                        {actionLabels[nextStatus]}
                      </button>
                    ))}
                  </section>
                )}
              </article>
            )
          })
        )}
      </section>

      <form className={styles.composer} onSubmit={addQuestion}>
        <label htmlFor="mock-question">목업 질문 추가</label>
        <textarea
          id="mock-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="예: 답변 완료 상태는 어떻게 보이나요?"
          rows={3}
        />
        {rejection && (
          <p className={styles.notice} role="alert">
            {rejectionMessages[rejection]}
          </p>
        )}
        <div className={styles.composerFooter}>
          <span>
            추가한 질문은 ‘대기 중’ 상태로 표시됩니다. 같은 대화에 실패한 질문이 있으면 정지 상태로 들어갑니다.
          </span>
          <button type="submit" disabled={!question.trim() || !activeConversation}>
            질문 추가
          </button>
        </div>
      </form>
    </main>
  )
}
