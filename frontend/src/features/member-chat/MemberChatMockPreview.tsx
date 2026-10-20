import { useMemo, useState, type FormEvent } from 'react'
import {
  initialMemberChatState,
  transitionMockTask,
  type ChatTaskStatus,
  type MemberChatMessage,
  type MemberChatMockState,
} from './memberChatMock'
import styles from './MemberChatMockPreview.module.css'

const statusLabels: Record<ChatTaskStatus, string> = {
  pending: '대기 중',
  running: '실행 중',
  paused: '일시 중지',
  completed: '완료',
  failed: '실패',
  interrupted: '중단됨',
  canceled: '취소됨',
  expired: '만료됨',
  'quota-exceeded': '할당량 초과',
}

function copyInitialState(): MemberChatMockState {
  return {
    ...initialMemberChatState,
    conversations: initialMemberChatState.conversations.map((conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => ({ ...message })),
    })),
  }
}

function createId(): string {
  return `mock-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export default function MemberChatMockPreview() {
  const [state, setState] = useState<MemberChatMockState>(copyInitialState)
  const [question, setQuestion] = useState('')
  const [activeConversationId, setActiveConversationId] = useState(state.activeConversationId)

  const activeConversation =
    state.conversations.find((conversation) => conversation.id === activeConversationId) ?? state.conversations[0]
  const messages = activeConversation?.messages ?? []

  const counts = useMemo(() => {
    return messages.reduce<Record<ChatTaskStatus, number>>(
      (result, message) => {
        result[message.status] += 1
        return result
      },
      {
        pending: 0,
        running: 0,
        paused: 0,
        completed: 0,
        failed: 0,
        interrupted: 0,
        canceled: 0,
        expired: 0,
        'quota-exceeded': 0,
      },
    )
  }, [messages])

  const updateMessage = (messageId: string, nextStatus: ChatTaskStatus) => {
    setState((current) => ({
      ...current,
      conversations: current.conversations.map((conversation) =>
        conversation.id !== activeConversationId
          ? conversation
          : {
              ...conversation,
              messages: conversation.messages.map((message) => {
                if (message.id !== messageId) return message
                const withAnswer: MemberChatMessage =
                  nextStatus === 'completed' && !message.answer
                    ? {
                        ...message,
                        answer: '목업 화면에서 완료 상태를 확인하기 위한 예시 답변입니다. 실제 서버 응답이 아닙니다.',
                      }
                    : message
                return transitionMockTask(withAnswer, nextStatus)
              }),
            },
      ),
    }))
  }

  const addQuestion = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const trimmed = question.trim()
    if (!trimmed || !activeConversation) return

    const newMessage: MemberChatMessage = {
      id: createId(),
      question: trimmed,
      status: 'pending',
      createdAt: new Date().toISOString(),
    }

    setState((current) => ({
      ...current,
      conversations: current.conversations.map((conversation) =>
        conversation.id === activeConversationId
          ? { ...conversation, messages: [...conversation.messages, newMessage] }
          : conversation,
      ),
    }))
    setQuestion('')
  }

  const reset = () => {
    const fresh = copyInitialState()
    setState(fresh)
    setActiveConversationId(fresh.activeConversationId)
    setQuestion('')
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
            <strong>{counts.pending}</strong>
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
      </section>

      <section className={styles.messages} aria-label="질문과 답변">
        <h2>{activeConversation?.title ?? '대화'}</h2>
        {messages.length === 0 ? (
          <div className={styles.empty}>아직 질문이 없습니다. 아래에서 목업 질문을 추가해 보세요.</div>
        ) : (
          messages.map((message) => (
            <article className={styles.messageCard} key={message.id}>
              <div className={styles.questionLabel}>질문</div>
              <p className={styles.question}>{message.question}</p>
              <div className={styles.statusRow}>
                <span className={`${styles.status} ${styles[`status_${message.status.replace('-', '_')}`]}`}>
                  {statusLabels[message.status]}
                </span>
                <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>
              </div>
              {message.answer && (
                <div className={styles.answer}>
                  <strong>답변</strong>
                  <p>{message.answer}</p>
                </div>
              )}
              <section className={styles.actions} aria-label="목업 상태 전이">
                <button
                  type="button"
                  onClick={() => updateMessage(message.id, 'pending')}
                  disabled={['completed', 'canceled', 'quota-exceeded'].includes(message.status)}
                >
                  대기로
                </button>
                <button
                  type="button"
                  onClick={() => updateMessage(message.id, 'running')}
                  disabled={['completed', 'canceled', 'quota-exceeded'].includes(message.status)}
                >
                  실행 중으로
                </button>
                <button
                  type="button"
                  onClick={() => updateMessage(message.id, 'completed')}
                  disabled={['completed', 'canceled', 'quota-exceeded'].includes(message.status)}
                >
                  완료로
                </button>
                <button
                  type="button"
                  onClick={() => updateMessage(message.id, 'failed')}
                  disabled={['completed', 'canceled', 'quota-exceeded'].includes(message.status)}
                >
                  실패로
                </button>
                <button
                  type="button"
                  onClick={() => updateMessage(message.id, 'canceled')}
                  disabled={['completed', 'canceled', 'quota-exceeded', 'running'].includes(message.status)}
                >
                  취소
                </button>
              </section>
            </article>
          ))
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
        <div className={styles.composerFooter}>
          <span>추가한 질문은 ‘대기 중’ 상태로 표시됩니다.</span>
          <button type="submit" disabled={!question.trim() || !activeConversation}>
            질문 추가
          </button>
        </div>
      </form>
    </main>
  )
}
