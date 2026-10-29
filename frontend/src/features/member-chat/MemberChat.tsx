import { useEffect, useLayoutEffect, useRef } from 'react'
import ChatAnswerView from './ChatAnswerView'
import ChatJobActions from './ChatJobActions'
import { selectAnswerView, type AnswerView } from './chatEventsReducer'
import { connectionLabels, jobStateLabels } from './chatJobLabels'
import type { ChatJobEnvelope } from './chatQueueTypes'
import type { ApiMessage } from './memberChatApiTypes'
import { QUESTION_MAX_CODE_POINTS, type ChatThread, type MemberChat as MemberChatState } from './useMemberChat'
import styles from './MemberChat.module.css'

/** 작업 없이 이력에만 있는 답변(#53 이전 시도 등)을 작업 답변과 같은 모양으로 보여 준다. */
function historyAnswerView(answer: ApiMessage): AnswerView {
  switch (answer.status) {
    case 'completed':
      return answer.content ? { kind: 'confirmed', content: answer.content } : { kind: 'unconfirmed' }
    case 'failed':
    case 'interrupted':
      return { kind: 'failed', state: answer.status, errorCode: answer.error_code }
    case 'pending':
      return { kind: 'unconfirmed' }
  }
}

/** 대기·정지·취소·만료처럼 답변이 없는 상태의 문구. 실행 중·완료·실패는 답변 영역이 보여 준다. */
function attemptLabel({ job }: ChatJobEnvelope): string {
  const parts: string[] = []
  if (job.kind === 'retry') parts.push('재시도')

  if (job.state === 'queued')
    parts.push(job.position ? `${jobStateLabels.queued} · ${job.position}번째` : jobStateLabels.queued)
  else if (job.state === 'paused' || job.state === 'canceled' || job.state === 'expired')
    parts.push(jobStateLabels[job.state])

  return parts.join(' · ')
}

function Thread({ thread, chat }: { thread: ChatThread; chat: MemberChatState }) {
  const { question, historyAnswers, jobs } = thread

  return (
    <div className={styles.thread} data-question-id={question.id}>
      <article className={styles.question}>
        <span>내 질문</span>
        <p>{question.content}</p>
      </article>

      <article className={styles.statusCard} aria-label="답변">
        {historyAnswers.map((answer) => (
          <div key={answer.id} className={styles.attempt}>
            {(answer.attempt_no ?? 1) > 1 && <p className={styles.attemptLabel}>재시도</p>}
            <ChatAnswerView view={historyAnswerView(answer)} />
          </div>
        ))}

        {jobs.map((envelope) => {
          const label = attemptLabel(envelope)

          return (
            <div key={envelope.job.id} className={styles.attempt} data-job-id={envelope.job.id}>
              {label && <p className={styles.attemptLabel}>{label}</p>}
              <ChatAnswerView view={selectAnswerView(chat.events, envelope.job.id)} />
              <ChatJobActions
                envelope={envelope}
                jobs={chat.events.jobs}
                limits={chat.events.limits}
                nowMs={chat.nowMs}
                commands={chat.commands}
              />
            </div>
          )
        })}

        {historyAnswers.length === 0 && jobs.length === 0 && (
          <p className={styles.attemptLabel} role="status">
            답변 상태를 확인하는 중입니다.
          </p>
        )}
      </article>
    </div>
  )
}

/** 맨 아래에서 이 거리 안에 있으면 사용자가 최신 내용을 보고 있다고 본다. */
const FOLLOW_THRESHOLD_PX = 80

/** 본문 스크롤 영역(`main`)을 끝까지 내린다. 입력창이 아래에 붙어 있어 끝부분을 화면 맨 아래에 맞추면 가려진다. */
function scrollToEnd(scroller: HTMLElement | null | undefined) {
  if (scroller) scroller.scrollTop = scroller.scrollHeight
}

type MemberChatProps = {
  chat: MemberChatState
}

/**
 * 회원 채팅 본문. 상태는 useMemberChat이 갖고, 사이드바와 같은 값을 보여 준다.
 * 답변은 구독 이벤트로 바뀐다. 생성 중 받은 조각은 임시 출력이고, 저장이 확인된 답변으로 교체된다.
 */
function MemberChat({ chat }: MemberChatProps) {
  const { events } = chat
  const bottomRef = useRef<HTMLDivElement>(null)
  // 사용자가 맨 아래 근처에서 최신 내용을 보고 있는지. 위로 올려 읽는 중이면 새 내용이 와도 끌어내리지 않는다.
  const followRef = useRef(true)

  useEffect(() => {
    const scroller = bottomRef.current?.closest('main')
    if (!scroller) return

    const update = () => {
      followRef.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= FOLLOW_THRESHOLD_PX
    }
    scroller.addEventListener('scroll', update, { passive: true })
    return () => scroller.removeEventListener('scroll', update)
  }, [])

  // 대화를 열거나 질문을 보낸 뒤에는 마지막 질문이 보이도록 맨 아래로 내리고, 이후 도착하는 답변을 따라간다.
  useEffect(() => {
    if (chat.scrollRequest === 0) return
    followRef.current = true
    scrollToEnd(bottomRef.current?.closest('main'))
  }, [chat.scrollRequest])

  // 구독으로 답변·임시 출력이 자라면, 맨 아래에서 보고 있을 때만 따라 내려간다. 그리기 직후에 맞춰 깜빡이지 않게 한다.
  useLayoutEffect(() => {
    if (followRef.current) scrollToEnd(bottomRef.current?.closest('main'))
  }, [chat.threads, events.drafts])

  const limits = events.limits
  const composerHint = chat.questionTooLong
    ? `질문은 ${QUESTION_MAX_CODE_POINTS.toLocaleString()}자까지 보낼 수 있습니다. (${chat.questionLength.toLocaleString()}자)`
    : chat.sending
      ? '질문 전송 중'
      : chat.resending
        ? '응답을 받지 못한 질문입니다. 다시 보내면 같은 질문으로 확인합니다.'
        : (chat.blockReason ?? '질문은 서버에 저장됩니다.')

  return (
    <section className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>TRAFFIC LAW AI</p>
          <h1>무엇이 궁금하신가요?</h1>
          <p>교통법 관련 질문을 입력해 주세요.</p>
        </div>
        <span className={styles.mockBadge} role="status" aria-label="실시간 연결 상태">
          실시간 {connectionLabels[events.connection]}
        </span>
      </header>

      {limits && (
        <p className={styles.limits}>
          오늘 남은 질문 {limits.daily.remaining}/{limits.daily.limit} · 6시간 남은 질문 {limits.six_hour.remaining}/
          {limits.six_hour.limit} · 새 질문 대기 {limits.queue.questions_waiting}/{limits.queue.questions_max} · 재시도
          대기 {limits.queue.retries_waiting}/{limits.queue.retries_max} · 실행 중 {limits.queue.running}
        </p>
      )}

      <div className={styles.chatArea} aria-live="polite">
        {chat.loading && <p>대화를 불러오는 중입니다.</p>}

        {!chat.loading && chat.threads.length === 0 && (
          <div className={styles.emptyState}>
            <h2>교통법에 대해 질문해 보세요</h2>
            <p>질문과 답변은 서버에 저장되어 다시 확인할 수 있습니다.</p>
          </div>
        )}

        {chat.threads.map((thread) => (
          <Thread key={thread.question.id} thread={thread} chat={chat} />
        ))}

        <div ref={bottomRef} />
      </div>

      {chat.error && (
        <div className={styles.error} role="alert">
          <p className={styles.errorText}>{chat.error}</p>
          {chat.errorReloadable && chat.conversationId && !chat.sending && (
            <button type="button" onClick={() => void chat.reloadConversation()}>
              대화 다시 불러오기
            </button>
          )}
        </div>
      )}

      <div className={styles.composerDock}>
        <form
          className={styles.composer}
          onSubmit={(event) => {
            event.preventDefault()
            void chat.submit()
          }}
        >
          <label htmlFor="chat-question">질문 입력</label>
          <textarea
            id="chat-question"
            value={chat.question}
            onChange={(event) => chat.setQuestion(event.target.value)}
            placeholder="교통법에 관해 궁금한 점을 입력하세요."
            rows={3}
            aria-describedby="chat-question-count"
            disabled={chat.sending}
          />
          <div className={styles.composerFooter}>
            <span id="chat-question-count">{composerHint}</span>
            <button type="submit" disabled={!chat.canSubmit || chat.sending}>
              {chat.sending ? '전송 중...' : chat.resending ? '다시 확인하기' : '질문 보내기'}
            </button>
          </div>
        </form>
      </div>
    </section>
  )
}

export default MemberChat
