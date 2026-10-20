import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { createConversation, listConversations, listMessages, sendMessage } from './memberChatApi'
import type { ApiConversation, ApiMessage } from './memberChatApiTypes'
import styles from './MemberChat.module.css'

function messageStatusLabel(message?: ApiMessage): string {
  if (!message) return '대기 중'

  switch (message.status) {
    case 'pending':
      return '답변 대기 중'
    case 'completed':
      return '답변 완료'
    case 'failed':
      return '생성 실패'
    case 'interrupted':
      return '응답 중단'
  }
}

function MemberChat() {
  const { accessToken } = useAuth()
  const [conversations, setConversations] = useState<ApiConversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ApiMessage[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const [pendingRequest, setPendingRequest] = useState<{
    conversationId: string
    content: string
    requestKey: string
  } | null>(null)

  const loadConversations = useCallback(async () => {
    if (!accessToken) return

    const result = await listConversations(accessToken)
    setConversations(result.items)
    return result.items
  }, [accessToken])

  const loadMessages = useCallback(
    async (id: string) => {
      if (!accessToken) return

      const result = await listMessages(accessToken, id)
      setMessages(result.items)
    },
    [accessToken],
  )

  useEffect(() => {
    if (!accessToken) {
      setConversations([])
      setConversationId(null)
      setMessages([])
      return
    }

    let cancelled = false

    async function load() {
      setLoading(true)
      setError('')

      try {
        const result = await listConversations(accessToken!)
        if (cancelled) return

        setConversations(result.items)

        if (result.items.length > 0) {
          const latest = result.items[0]
          setConversationId(latest.id)

          const history = await listMessages(accessToken!, latest.id)
          if (!cancelled) setMessages(history.items)
        } else {
          setConversationId(null)
          setMessages([])
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '대화 목록을 불러오지 못했습니다.')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void load()

    return () => {
      cancelled = true
    }
  }, [accessToken])

  const selectConversation = async (id: string) => {
    if (!accessToken) return

    setConversationId(id)
    setMessages([])
    setError('')
    setLoading(true)

    try {
      await loadMessages(id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '메시지를 불러오지 못했습니다.')
    } finally {
      setLoading(false)
    }
  }

  const submitQuestion = async () => {
    const content = question.trim()
    if (!content || !accessToken || sending) return

    setSending(true)
    setError('')

    try {
      // 같은 질문의 전송이 실패했다면 기존 요청을 재사용한다.
      const existingRequest = pendingRequest?.content === content ? pendingRequest : null

      let activeId = existingRequest?.conversationId ?? conversationId

      if (!activeId) {
        const conversation = await createConversation(accessToken)
        activeId = conversation.id
        setConversationId(activeId)
        setConversations((previous) => [conversation, ...previous])
      }

      const request = existingRequest ?? {
        conversationId: activeId,
        content,
        requestKey: crypto.randomUUID(),
      }

      // 전송 전에 요청 키를 보관한다.
      setPendingRequest(request)

      await sendMessage(accessToken, request.conversationId, {
        content: request.content,
        request_key: request.requestKey,
      })

      // 서버가 요청을 접수했으므로 재전송용 키를 정리한다.
      setPendingRequest(null)
      setQuestion('')

      // POST 응답 이후 저장된 메시지 이력을 다시 읽는다.
      await loadMessages(request.conversationId)
      await loadConversations()
    } catch (err) {
      // 실패 시 pendingRequest를 유지해 다음 시도에서 재사용한다.
      setError(err instanceof Error ? err.message : '질문 전송에 실패했습니다.')
    } finally {
      setSending(false)
    }
  }

  const answersByQuestion = new Map<string, ApiMessage>()

  for (const message of messages) {
    if (message.role === 'assistant' && message.reply_to_message_id) {
      const previous = answersByQuestion.get(message.reply_to_message_id)

      if (!previous || (message.attempt_no ?? 0) > (previous.attempt_no ?? 0)) {
        answersByQuestion.set(message.reply_to_message_id, message)
      }
    }
  }

  return (
    <section className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>TRAFFIC LAW AI</p>
          <h1>무엇이 궁금하신가요?</h1>
          <p>교통법 관련 질문을 입력해 주세요.</p>
        </div>
        <span className={styles.mockBadge}>API 연결</span>
      </header>

      <section className={styles.queuePanel} aria-label="내 대화">
        <div>
          <h2>내 대화</h2>
          <p>서버에 저장된 대화 목록입니다.</p>
        </div>

        <div>
          <label htmlFor="conversation-select">대화 선택</label>
          <select
            id="conversation-select"
            value={conversationId ?? ''}
            onChange={(event) => void selectConversation(event.target.value)}
            disabled={loading || conversations.length === 0}
          >
            {conversations.length === 0 && <option value="">새 질문을 입력해 대화를 시작하세요</option>}
            {conversations.map((conversation) => (
              <option key={conversation.id} value={conversation.id}>
                대화 {conversation.id}
              </option>
            ))}
          </select>

          <button
            type="button"
            disabled={!accessToken || loading || sending}
            onClick={() => {
              setConversationId(null)
              setMessages([])
              setError('')
              setPendingRequest(null)
            }}
          >
            새 대화
          </button>
        </div>
      </section>

      <div className={styles.chatArea} aria-live="polite">
        {loading && <p>대화를 불러오는 중입니다.</p>}

        {!loading && messages.length === 0 && (
          <div className={styles.emptyState}>
            <h2>교통법에 대해 질문해 보세요</h2>
            <p>질문과 답변은 서버에 저장되어 다시 확인할 수 있습니다.</p>
          </div>
        )}

        {messages
          .filter((message) => message.role === 'user')
          .map((message) => {
            const answer = answersByQuestion.get(message.id)

            return (
              <div key={message.id}>
                <article className={styles.question}>
                  <span>내 질문</span>
                  <p>{message.content}</p>
                </article>

                <article className={styles.statusCard}>
                  <strong>{messageStatusLabel(answer)}</strong>

                  {answer?.status === 'completed' && answer.content && (
                    <div className={styles.answer}>{answer.content}</div>
                  )}

                  {answer?.status === 'failed' && <p>답변 생성에 실패했습니다. 오류 코드: {answer.error_code}</p>}

                  {answer?.status === 'interrupted' && (
                    <p>답변 생성이 중단되었습니다. 오류 코드: {answer.error_code}</p>
                  )}

                  {(!answer || answer.status === 'pending') && <p>답변 상태를 다시 확인해 주세요.</p>}
                </article>
              </div>
            )
          })}
      </div>

      {error && <p role="alert">{error}</p>}

      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault()
          void submitQuestion()
        }}
      >
        <label htmlFor="chat-question">질문 입력</label>
        <textarea
          id="chat-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="교통법에 관해 궁금한 점을 입력하세요."
          rows={3}
          maxLength={4000}
          disabled={!accessToken || sending}
        />
        <div className={styles.composerFooter}>
          <span>{sending ? '질문 전송 중' : '질문은 서버에 저장됩니다.'}</span>
          <button type="submit" disabled={!accessToken || !question.trim() || sending}>
            {sending ? '전송 중...' : '질문 보내기'}
          </button>
        </div>
      </form>
    </section>
  )
}

export default MemberChat
