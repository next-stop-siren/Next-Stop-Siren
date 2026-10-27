import { useReducer, useRef, useState, type FormEvent } from 'react'
import ChatAnswerView from './ChatAnswerView'
import ChatJobActions from './ChatJobActions'
import { selectAnswerView } from './chatEventsReducer'
import { connectionLabels, jobStateLabels as stateLabels } from './chatJobLabels'
import type { ChatJobEnvelope } from './chatQueueTypes'
import { createMockChatServer } from './mockChatServer'
import { useChatEvents } from './useChatEvents'
import { useChatJobCommands } from './useChatJobCommands'
import styles from './MockStreamingPanel.module.css'

const CHUNKS = ['도로교통법 제160조에 따라 ', '어린이 보호구역의 ', '주정차 위반은 과태료 부과 대상입니다.']

type MockStreamingPanelProps = {
  /** 끊긴 뒤 다시 연결하기까지의 시간. 기본값은 구독 훅의 기본 간격이다. */
  reconnectDelayMs?: (attempt: number) => number
}

/**
 * 스트리밍 임시 출력과 확정 답변 화면(#20), 재시도·건너뛰기 버튼(#21)을 가짜 서버로 확인하는 개발용 패널.
 * 서버 쪽 동작(실행 시작·조각·저장·실패)은 "목업 서버 조작" 버튼으로 직접 일으킨다.
 * 실제 API·AI·대기열을 쓰지 않으며, 구독 훅과 화면은 실제 연동 때 그대로 쓴다.
 */
function MockStreamingPanel({ reconnectDelayMs }: MockStreamingPanelProps) {
  const [server] = useState(createMockChatServer)
  const [, refresh] = useReducer((count: number) => count + 1, 0)
  const [signedIn, setSignedIn] = useState(true)
  const [question, setQuestion] = useState('')
  const [notice, setNotice] = useState('')
  const chunkCounts = useRef<Record<string, number>>({})

  const state = useChatEvents({
    userId: signedIn ? 'mock-user' : null,
    getAccessToken: () => 'mock-token',
    open: server.open,
    resolveJob: async (_accessToken, jobId) => server.getEnvelope(jobId),
    reconnectDelayMs,
  })

  // 재시도·건너뛰기·취소는 사용자가 누르는 동작이다. 화면은 응답이 아니라 구독 이벤트로 바뀐다.
  const commands = useChatJobCommands(
    signedIn ? 'mock-user' : null,
    {
      retry: async (jobId, retryKey) => {
        try {
          return server.retry(jobId, retryKey)
        } finally {
          refresh()
        }
      },
      skip: async (jobId) => {
        try {
          server.skip(jobId)
        } finally {
          refresh()
        }
      },
      cancel: async (jobId) => {
        try {
          server.cancel(jobId)
        } finally {
          refresh()
        }
      },
    },
    // 서버가 거절했다면 화면이 뒤처졌을 수 있으니 그 작업을 다시 읽는다.
    { onFailure: state.refreshJob },
  )

  // 서버 쪽 조작은 서버 상태만 바꾸므로, 서버에 남은 작업 수를 다시 그리도록 갱신한다.
  const operate = (action: () => void) => {
    try {
      action()
      setNotice('')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '조작에 실패했습니다.')
    }
    refresh()
  }

  const accept = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const content = question.trim()
    if (!content) return

    operate(() => {
      server.accept('mock-conversation', content)
      setQuestion('')
    })
  }

  const sendChunk = (jobId: string) =>
    operate(() => {
      const index = chunkCounts.current[jobId] ?? 0
      server.emitDelta(jobId, CHUNKS[index % CHUNKS.length] ?? '')
      chunkCounts.current[jobId] = index + 1
    })

  // 같은 질문의 시도(처음 답변과 재시도)를 한 묶음으로 보여 준다.
  const groups = new Map<string, ChatJobEnvelope[]>()
  for (const envelope of Object.values(state.jobs).sort((a, b) => Number(a.job.id) - Number(b.job.id))) {
    const key = envelope.job.question_message_id
    groups.set(key, [...(groups.get(key) ?? []), envelope])
  }

  const nowMs = Date.parse(server.serverTime())

  return (
    <section className={styles.panel} aria-label="스트리밍 임시 출력 확인">
      <header className={styles.header}>
        <div>
          <h2>스트리밍 · 확정 답변 목업</h2>
          <p>받은 조각은 점선 상자의 임시 출력으로만 보이고, 서버가 답변을 저장해 알린 뒤에야 확정 답변이 됩니다.</p>
        </div>
        <span className={styles.badge}>실제 API 미연결</span>
      </header>

      <div className={styles.toolbar}>
        <span role="status">구독: {connectionLabels[state.connection]}</span>
        <button type="button" onClick={() => operate(() => server.disconnectAll())} disabled={!signedIn}>
          연결 끊기 (탭 닫기 흉내)
        </button>
        <button type="button" onClick={() => operate(() => server.advance(10))} disabled={!signedIn}>
          10초 보내기 (목업 시계)
        </button>
        <button type="button" onClick={() => setSignedIn((current) => !current)}>
          {signedIn ? '로그아웃' : '다시 로그인'}
        </button>
      </div>

      <p className={styles.note}>
        서버에는 작업 {server.listJobs().length}개가 있습니다. 연결을 끊거나 로그아웃해도 작업은 계속됩니다.
      </p>

      {!signedIn ? (
        <p className={styles.empty}>로그아웃 상태입니다. 화면의 대화·임시 출력·구독을 모두 지웠습니다.</p>
      ) : (
        <>
          <form className={styles.form} onSubmit={accept}>
            <label htmlFor="mock-stream-question">스트리밍 목업 질문</label>
            <input
              id="mock-stream-question"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="예: 어린이보호구역 제한속도는 얼마야?"
            />
            <button type="submit" disabled={!question.trim()}>
              질문 접수
            </button>
          </form>

          {notice && (
            <p className={styles.notice} role="alert">
              {notice}
            </p>
          )}

          {state.limits && (
            <p className={styles.note}>
              재시도 대기 {state.limits.queue.retries_waiting}/{state.limits.queue.retries_max} · 새 질문 대기{' '}
              {state.limits.queue.questions_waiting}/{state.limits.queue.questions_max}
            </p>
          )}

          {groups.size === 0 ? (
            <p className={styles.empty}>접수한 질문이 없습니다.</p>
          ) : (
            [...groups.values()].map((attempts) => (
              <article className={styles.job} key={attempts[0]?.job.question_message_id}>
                <p className={styles.question}>{attempts[0]?.question.content}</p>

                {attempts.map(({ job }) => (
                  <div key={job.id} data-job-id={job.id}>
                    <p className={styles.state}>
                      {job.kind === 'retry' ? '재시도 · ' : ''}
                      {stateLabels[job.state]}
                      {job.position ? ` · ${job.position}번째` : ''}
                    </p>

                    <ChatAnswerView view={selectAnswerView(state, job.id)} />

                    <ChatJobActions
                      envelope={state.jobs[job.id] as ChatJobEnvelope}
                      jobs={state.jobs}
                      limits={state.limits}
                      nowMs={nowMs}
                      commands={commands}
                    />

                    {(job.state === 'queued' || job.state === 'paused' || job.state === 'running') && (
                      <fieldset className={styles.controls}>
                        <legend className={styles.legend}>목업 서버 조작</legend>
                        {job.state === 'queued' && (
                          <button type="button" onClick={() => operate(() => server.start(job.id))}>
                            실행 시작
                          </button>
                        )}
                        {(job.state === 'queued' || job.state === 'paused') && (
                          <button type="button" onClick={() => operate(() => server.expire(job.id))}>
                            5분 경과로 만료
                          </button>
                        )}
                        {job.state === 'running' && (
                          <>
                            <button type="button" onClick={() => sendChunk(job.id)}>
                              조각 보내기
                            </button>
                            <button type="button" onClick={() => operate(() => server.complete(job.id))}>
                              답변 저장
                            </button>
                            <button type="button" onClick={() => operate(() => server.fail(job.id))}>
                              실패로 끝내기
                            </button>
                            <button type="button" onClick={() => operate(() => server.interrupt(job.id))}>
                              중단으로 끝내기
                            </button>
                          </>
                        )}
                      </fieldset>
                    )}
                  </div>
                ))}
              </article>
            ))
          )}
        </>
      )}
    </section>
  )
}

export default MockStreamingPanel
