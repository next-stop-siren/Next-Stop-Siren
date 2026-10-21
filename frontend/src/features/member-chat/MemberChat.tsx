import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { createConversation, listAllConversations, listAllMessages, sendMessage } from './memberChatApi'
import type { ApiConversation, ApiMessage } from './memberChatApiTypes'
import styles from './MemberChat.module.css'

type PendingRequest = {
  conversationId: string
  content: string
  requestKey: string
}

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

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

// API 명세의 질문 길이 한도다. 서버는 Unicode 코드 포인트 기준으로 세므로 UTF-16 길이(.length)를 쓰지 않는다.
const QUESTION_MAX_CODE_POINTS = 4000

function countCodePoints(text: string): number {
  return Array.from(text).length
}

function MemberChat() {
  const { accessToken, user } = useAuth()
  const userId = user?.id ?? null

  const [conversations, setConversations] = useState<ApiConversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ApiMessage[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  // access 토큰은 약 15분마다 새 값으로 바뀐다. 토큰이 바뀔 때마다 대화를 다시 불러오면
  // 선택한 대화가 초기화되므로, 호출 시점의 최신 값을 ref로 읽고 효과는 사용자 기준으로만 건다.
  const accessTokenRef = useRef(accessToken)
  useEffect(() => {
    accessTokenRef.current = accessToken
  }, [accessToken])

  // 사용자가 바뀌거나 화면이 사라지면 올라가는 값이다. 늦게 도착한 이전 응답을 버리는 기준이다.
  const epochRef = useRef(0)
  // 화면에 보이는 대화의 최신 값이다. 대화를 빠르게 바꿀 때 이전 대화의 응답이 덮어쓰지 못하게 한다.
  const conversationIdRef = useRef<string | null>(null)
  // 결과를 확인하지 못한 전송 요청을 대화별로 보관한다. 같은 대화에서 같은 본문을 다시 보낼 때만 같은 키를 쓰므로,
  // 다른 대화를 다녀와도 응답을 잃은 질문의 키를 잃지 않는다. 화면에 그리지 않아 ref로 둔다.
  // 새로고침 뒤에는 서버 이력(질문의 request_key 포함)에서 접수된 질문과 상태를 복구한다.
  const pendingRequestsRef = useRef(new Map<string, PendingRequest>())
  // 메시지 조회가 겹칠 때 가장 나중에 시작한 조회의 결과만 화면에 반영하기 위한 순번이다.
  const messagesSeqRef = useRef(0)
  // 사용자가 목록을 읽는 동안 대화를 고르거나 새로 만들었는지 나타낸다. 그렇다면 첫 목록이 선택을 덮어쓰지 않는다.
  const navigatedRef = useRef(false)
  // 전송 중 여부를 동기적으로 막는 가드다. state는 다시 그려져야 바뀌므로, 엔터 연타나 더블클릭이 겹치면
  // 두 번째 제출이 통과해 (대화가 없을 때) 대화가 둘 만들어질 수 있다.
  const sendingRef = useRef(false)

  const selectId = useCallback((id: string | null) => {
    conversationIdRef.current = id
    setConversationId(id)
  }, [])

  const refreshMessages = useCallback(async (id: string) => {
    const token = accessTokenRef.current
    if (!token) return

    const epoch = epochRef.current
    messagesSeqRef.current += 1
    const seq = messagesSeqRef.current
    const items = await listAllMessages(token, id)

    if (epochRef.current === epoch && conversationIdRef.current === id && messagesSeqRef.current === seq) {
      setMessages(items)
    }
  }, [])

  useEffect(() => {
    epochRef.current += 1
    const epoch = epochRef.current
    const isStale = () => epochRef.current !== epoch

    // 사용자가 바뀌면 새 목록을 읽기 전에 이전 사용자의 요청 키와 화면 상태(대화·초안·전송 상태)를 모두 비운다.
    // 새 목록 조회가 실패해도 이전 사용자의 내용이 남지 않고, 전송 중이던 입력창이 잠긴 채로 남지도 않는다.
    pendingRequestsRef.current.clear()
    navigatedRef.current = false
    messagesSeqRef.current += 1
    sendingRef.current = false
    setConversations([])
    selectId(null)
    setMessages([])
    setQuestion('')
    setSending(false)
    setLoading(false)
    setError('')

    const token = accessTokenRef.current

    if (!userId || !token) return

    async function load(accessToken: string) {
      setLoading(true)

      try {
        const items = await listAllConversations(accessToken)
        if (isStale()) return

        // 목록을 읽는 동안 새로 만든 대화가 있으면 그대로 두고, 서버 목록에서 아직 없는 것만 뒤에 붙인다.
        setConversations((current) => [...current, ...items.filter((item) => !current.some((c) => c.id === item.id))])

        // 목록을 읽는 동안 사용자가 대화를 고르거나 새로 만들었다면 그 선택을 존중한다.
        if (navigatedRef.current) return

        const latest = items[0]

        if (latest) {
          selectId(latest.id)
          await refreshMessages(latest.id)
        }
      } catch (err) {
        if (!isStale()) setError(errorText(err, '대화 목록을 불러오지 못했습니다.'))
      } finally {
        if (!isStale()) setLoading(false)
      }
    }

    void load(token)

    return () => {
      epochRef.current += 1
    }
  }, [userId, selectId])

  const selectConversation = async (id: string) => {
    if (!accessTokenRef.current) return

    navigatedRef.current = true
    selectId(id)
    setMessages([])
    setError('')
    setLoading(true)

    try {
      await refreshMessages(id)
    } catch (err) {
      if (conversationIdRef.current === id) setError(errorText(err, '메시지를 불러오지 못했습니다.'))
    } finally {
      // 그 사이 다른 대화로 바뀌었다면 로딩 표시는 새로 선택한 대화가 관리한다.
      if (conversationIdRef.current === id) setLoading(false)
    }
  }

  const reloadCurrentConversation = async () => {
    const id = conversationIdRef.current
    if (!id) return

    setError('')

    try {
      await refreshMessages(id)
    } catch (err) {
      if (conversationIdRef.current === id) setError(errorText(err, '답변 상태를 불러오지 못했습니다.'))
    }
  }

  const startNewConversation = () => {
    // 보관 중인 요청 키는 지우지 않는다. 키는 대화별로 보관되어 같은 대화·같은 질문일 때만 다시 쓰이므로
    // 다른 대화로 잘못 전송되지 않고, 응답을 잃은 질문의 키도 잃지 않는다.
    navigatedRef.current = true
    selectId(null)
    setMessages([])
    setError('')
  }

  const questionLength = countCodePoints(question)
  const questionTooLong = questionLength > QUESTION_MAX_CODE_POINTS
  const canSubmit = question.trim().length > 0 && !questionTooLong

  const submitQuestion = async () => {
    // 서버는 질문 원문을 보존하므로 앞뒤 공백을 지우지 않고 그대로 보낸다. trim은 공백뿐인지 확인할 때만 쓴다.
    const content = question
    const token = accessTokenRef.current
    if (!canSubmit || !token || sending || sendingRef.current) return

    const epoch = epochRef.current

    sendingRef.current = true
    setSending(true)
    setError('')

    let accepted: PendingRequest | null = null

    try {
      let activeId = conversationIdRef.current

      if (!activeId) {
        const conversation = await createConversation(token)
        if (epochRef.current !== epoch) return

        activeId = conversation.id
        navigatedRef.current = true
        selectId(activeId)
        setConversations((previous) => [conversation, ...previous])
      }

      // 같은 대화에서 같은 질문을 다시 보낼 때만 이전 키를 재사용한다. 다른 대화의 보관 키는 건드리지 않는다.
      const pending = pendingRequestsRef.current.get(activeId)
      const request: PendingRequest =
        pending && pending.content === content
          ? pending
          : { conversationId: activeId, content, requestKey: crypto.randomUUID() }

      // 전송 전에 요청 키를 보관한다.
      pendingRequestsRef.current.set(activeId, request)

      await sendMessage(accessTokenRef.current ?? token, request.conversationId, {
        content: request.content,
        request_key: request.requestKey,
      })

      if (epochRef.current !== epoch) return

      // 서버가 요청을 접수했으므로 이 대화의 재전송용 키를 정리한다.
      pendingRequestsRef.current.delete(request.conversationId)
      accepted = request
      setQuestion('')
    } catch (err) {
      // 실패 시 보관한 키를 유지해 다음 시도에서 재사용한다.
      if (epochRef.current === epoch) setError(errorText(err, '질문 전송에 실패했습니다.'))
    } finally {
      if (epochRef.current === epoch) {
        sendingRef.current = false
        setSending(false)
      }
    }

    if (!accepted) return

    // POST 응답 이후 저장된 메시지 이력을 다시 읽는다. 대화 목록은 생성순이라 순서가 바뀌지 않는다.
    try {
      await refreshMessages(accepted.conversationId)
    } catch {
      if (epochRef.current === epoch) {
        setError('질문은 접수됐지만 대화를 다시 불러오지 못했습니다. 답변 상태를 새로고침해 주세요.')
      }
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
            disabled={loading || sending || conversations.length === 0}
          >
            {conversations.length === 0 && <option value="">새 질문을 입력해 대화를 시작하세요</option>}
            {conversations.map((conversation) => (
              <option key={conversation.id} value={conversation.id}>
                대화 {conversation.id}
              </option>
            ))}
          </select>

          <button type="button" disabled={!accessToken || loading || sending} onClick={startNewConversation}>
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

                  {(!answer || answer.status === 'pending') && (
                    <>
                      <p>답변 상태를 다시 확인해 주세요.</p>
                      <button type="button" onClick={() => void reloadCurrentConversation()}>
                        답변 상태 새로고침
                      </button>
                    </>
                  )}
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
          aria-describedby="chat-question-count"
          disabled={!accessToken || sending}
        />
        <div className={styles.composerFooter}>
          <span id="chat-question-count">
            {questionTooLong
              ? `질문은 ${QUESTION_MAX_CODE_POINTS.toLocaleString()}자까지 보낼 수 있습니다. (${questionLength.toLocaleString()}자)`
              : sending
                ? '질문 전송 중'
                : '질문은 서버에 저장됩니다.'}
          </span>
          <button type="submit" disabled={!accessToken || !canSubmit || sending}>
            {sending ? '전송 중...' : '질문 보내기'}
          </button>
        </div>
      </form>
    </section>
  )
}

export default MemberChat
