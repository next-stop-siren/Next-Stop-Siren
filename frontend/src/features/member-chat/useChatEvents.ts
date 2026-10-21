import { useEffect, useReducer, useRef } from 'react'
import { openChatEvents, type OpenChatEvents } from './chatEventStream'
import { chatEventsReducer, initialChatEventsState, type ChatEventsState } from './chatEventsReducer'
import { fetchChatJob } from './chatQueueApi'
import type { ChatJobEnvelope } from './chatQueueTypes'

export interface UseChatEventsOptions {
  /** 로그인한 회원 ID. 바뀌거나 null이 되면 이전 회원의 구독과 비공개 상태를 모두 버린다. */
  userId: string | null
  /** 연결할 때마다 호출해 그 시점의 최신 access 토큰을 쓴다. 토큰이 갱신돼도 연결을 새로 열지 않는다. */
  getAccessToken: () => string | null
  open?: OpenChatEvents
  resolveJob?: (accessToken: string, jobId: string) => Promise<ChatJobEnvelope | null>
  /** 끊긴 뒤 다시 연결하기까지 기다릴 시간. 연결 실패가 이어질수록 늘린다. */
  reconnectDelayMs?: (attempt: number) => number
}

const MAX_RECONNECT_DELAY_MS = 15_000

export function defaultReconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, MAX_RECONNECT_DELAY_MS)
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve()
      return
    }

    function done() {
      clearTimeout(timer)
      signal.removeEventListener('abort', done)
      resolve()
    }

    const timer = setTimeout(done, ms)
    signal.addEventListener('abort', done, { once: true })
  })
}

/**
 * 회원 단위 구독을 열어 이벤트를 화면 상태로 모은다.
 *
 * - 연결이 끊기면 마지막 snapshot의 server_time을 `since`로 보내 다시 연결한다. 끊긴 사이 끝난 답변과
 *   바뀐 버튼은 새 snapshot의 저장된 결과로 받는다.
 * - 연결을 끊거나 다시 여는 일은 서버 작업에 영향이 없다. 이 훅은 질문을 다시 제출하지 않는다.
 * - 사용자가 바뀌거나 로그아웃하면 구독을 끊고 대화·임시 출력·작업 상태를 모두 지운다.
 */
export function useChatEvents(options: UseChatEventsOptions): ChatEventsState {
  const { userId } = options
  const [state, dispatch] = useReducer(chatEventsReducer, initialChatEventsState)

  // 연결 루프는 userId로만 다시 시작한다. 나머지 옵션은 호출 시점의 최신 값을 ref로 읽는다.
  const optionsRef = useRef(options)
  useEffect(() => {
    optionsRef.current = options
  })

  const sinceRef = useRef<string | null>(null)
  const epochRef = useRef(0)
  const resolvingRef = useRef(new Set<string>())

  useEffect(() => {
    epochRef.current += 1
    sinceRef.current = null
    resolvingRef.current.clear()
    dispatch({ type: 'reset' })

    if (!userId) return

    const controller = new AbortController()
    const { signal } = controller

    async function run() {
      let attempt = 0
      let everOpened = false

      while (!signal.aborted) {
        const { getAccessToken, open = openChatEvents, reconnectDelayMs = defaultReconnectDelayMs } = optionsRef.current
        const accessToken = getAccessToken()

        if (accessToken) {
          dispatch({ type: 'connection', status: everOpened ? 'reconnecting' : 'connecting' })

          try {
            await open({
              accessToken,
              since: sinceRef.current,
              signal,
              onEvent: (event) => {
                if (signal.aborted) return

                if (event.type === 'snapshot') sinceRef.current = event.data.server_time

                everOpened = true
                attempt = 0
                dispatch({ type: 'connection', status: 'open' })
                dispatch({ type: 'event', event })
              },
            })
          } catch {
            // 연결 거절·형식 오류·네트워크 끊김은 모두 같은 방식으로 다시 연결한다.
          }
        }

        if (signal.aborted) return

        dispatch({ type: 'connection', status: 'reconnecting' })
        await sleep(reconnectDelayMs(attempt), signal)
        attempt += 1
      }
    }

    void run()

    return () => {
      controller.abort()
      dispatch({ type: 'reset' })
    }
  }, [userId])

  // 진행 중으로 알던 작업이 snapshot에 없으면 최종 결과를 따로 조회한다.
  useEffect(() => {
    if (!userId) return

    const epoch = epochRef.current

    for (const jobId of state.unresolved) {
      if (resolvingRef.current.has(jobId)) continue

      const { getAccessToken, resolveJob = fetchChatJob } = optionsRef.current
      const accessToken = getAccessToken()
      if (!accessToken) continue

      resolvingRef.current.add(jobId)

      resolveJob(accessToken, jobId)
        .then((envelope) => {
          if (epochRef.current === epoch && envelope)
            dispatch({ type: 'event', event: { type: 'job', data: envelope } })
        })
        .catch(() => {
          // 조회에 실패하면 unresolved에 남는다. 다음 재연결 snapshot이 저장된 결과를 다시 준다.
        })
        .finally(() => {
          resolvingRef.current.delete(jobId)
        })
    }
  }, [state.unresolved, userId])

  return state
}
