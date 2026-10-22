import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
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
  /** 연결이 이 시간 이상 이어진 뒤에 끊겨야 재연결 간격을 처음으로 되돌린다. 기본 10초. */
  stableConnectionMs?: number
  /** 작업 조회가 실패하거나 답변이 비어 있을 때 다시 조회하기까지 기다릴 시간. 기본 3초. */
  resolveRetryMs?: number
}

export interface ChatEventsView extends ChatEventsState {
  /**
   * 작업 하나를 지금 서버 상태로 다시 읽어 화면에 반영한다. 버튼을 눌렀는데 서버가 거절했을 때처럼,
   * 구독 이벤트를 놓쳐 화면이 뒤처졌을 수 있을 때 쓴다. 실패하면 아무것도 바꾸지 않는다.
   */
  refreshJob: (jobId: string) => Promise<void>
}

const MAX_RECONNECT_DELAY_MS = 15_000
const DEFAULT_STABLE_CONNECTION_MS = 10_000
const DEFAULT_RESOLVE_RETRY_MS = 3_000
/** 같은 작업을 이 횟수까지 조회해 보고, 그래도 안 되면 다음 snapshot을 기다린다. */
export const MAX_RESOLVE_ATTEMPTS = 5

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
export function useChatEvents(options: UseChatEventsOptions): ChatEventsView {
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
  // 작업별 조회 횟수, 다음 조회까지 쉬는 중인 작업, 그 타이머. 사용자가 바뀌면 모두 버린다.
  const attemptsRef = useRef(new Map<string, number>())
  const cooldownRef = useRef(new Set<string>())
  const timersRef = useRef(new Set<ReturnType<typeof setTimeout>>())
  const lastServerTimeRef = useRef<string | null>(null)
  // 구독 이벤트를 받을 때마다 올라가는 번호와, 작업별로 마지막 이벤트를 받은 번호.
  // 작업 조회를 시작한 뒤 같은 작업의 이벤트가 먼저 오면, 늦게 온 조회 결과는 오래된 상태라 버린다.
  const eventSeqRef = useRef(0)
  const lastEventSeqRef = useRef(new Map<string, number>())
  const [resolveTick, bumpResolveTick] = useReducer((count: number) => count + 1, 0)

  useEffect(() => {
    epochRef.current += 1
    sinceRef.current = null
    resolvingRef.current.clear()
    attemptsRef.current.clear()
    cooldownRef.current.clear()
    lastServerTimeRef.current = null
    lastEventSeqRef.current.clear()
    for (const timer of timersRef.current) clearTimeout(timer)
    timersRef.current.clear()
    dispatch({ type: 'reset' })

    if (!userId) return

    const controller = new AbortController()
    const { signal } = controller

    async function run() {
      let attempt = 0
      let everOpened = false

      while (!signal.aborted) {
        const {
          getAccessToken,
          open = openChatEvents,
          reconnectDelayMs = defaultReconnectDelayMs,
          stableConnectionMs = DEFAULT_STABLE_CONNECTION_MS,
        } = optionsRef.current
        const accessToken = getAccessToken()
        let openedAt: number | null = null

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

                // 이 이벤트가 바꾼 작업을 기록해 둔다. 진행 중이던 작업 조회 결과가 이보다 늦게 오면 버린다.
                const seq = ++eventSeqRef.current
                if (event.type === 'snapshot') {
                  for (const item of event.data.items) lastEventSeqRef.current.set(item.job.id, seq)
                } else if (event.type === 'job') {
                  lastEventSeqRef.current.set(event.data.job.id, seq)
                }

                everOpened = true
                openedAt ??= Date.now()
                dispatch({ type: 'connection', status: 'open' })
                dispatch({ type: 'event', event })
              },
            })
          } catch {
            // 연결 거절·형식 오류·오래 조용한 연결·네트워크 끊김은 모두 같은 방식으로 다시 연결한다.
          }
        }

        if (signal.aborted) return

        // 이벤트를 한 번 받았다는 것만으로는 건강한 연결이 아니다. 서버가 snapshot만 보내고 바로 끊는 상황이
        // 이어져도 간격이 늘어나도록, 충분히 오래 이어진 연결일 때만 간격을 처음으로 되돌린다.
        if (openedAt !== null && Date.now() - openedAt >= stableConnectionMs) attempt = 0

        dispatch({ type: 'connection', status: 'reconnecting' })
        await sleep(reconnectDelayMs(attempt), signal)
        attempt += 1
      }
    }

    void run()

    return () => {
      controller.abort()
      for (const timer of timersRef.current) clearTimeout(timer)
      timersRef.current.clear()
      dispatch({ type: 'reset' })
    }
  }, [userId])

  // 최종 결과를 따로 조회해야 하는 작업(snapshot에서 빠진 진행 중 작업, 답변이 비어 온 완료 작업)을 조회한다.
  // 조회가 실패하거나 결과가 여전히 비어 있으면 잠시 쉬었다가 다시 조회하고, 횟수가 차면 다음 snapshot을 기다린다.
  useEffect(() => {
    if (!userId) return

    const epoch = epochRef.current

    // 새 snapshot이 오면 조회 횟수를 처음부터 센다. 이제 끝난 작업은 기록에서 지운다.
    if (lastServerTimeRef.current !== state.serverTime) {
      lastServerTimeRef.current = state.serverTime
      attemptsRef.current.clear()
    }
    for (const jobId of [...attemptsRef.current.keys()]) {
      if (!state.unresolved.includes(jobId)) attemptsRef.current.delete(jobId)
    }

    for (const jobId of state.unresolved) {
      if (resolvingRef.current.has(jobId) || cooldownRef.current.has(jobId)) continue

      const attempts = attemptsRef.current.get(jobId) ?? 0
      if (attempts >= MAX_RESOLVE_ATTEMPTS) continue

      const {
        getAccessToken,
        resolveJob = fetchChatJob,
        resolveRetryMs = DEFAULT_RESOLVE_RETRY_MS,
      } = optionsRef.current
      const accessToken = getAccessToken()
      if (!accessToken) continue

      attemptsRef.current.set(jobId, attempts + 1)
      resolvingRef.current.add(jobId)
      const startedAt = eventSeqRef.current

      resolveJob(accessToken, jobId)
        .then((envelope) => {
          if (epochRef.current !== epoch || !envelope) return
          // 조회하는 동안 같은 작업의 구독 이벤트가 왔다면 그쪽이 더 최신이다.
          if ((lastEventSeqRef.current.get(jobId) ?? 0) > startedAt) return

          dispatch({ type: 'event', event: { type: 'job', data: envelope } })
        })
        .catch(() => {
          // 실패는 아래에서 잠시 쉰 뒤 다시 조회한다.
        })
        .finally(() => {
          resolvingRef.current.delete(jobId)
          if (epochRef.current !== epoch) return

          cooldownRef.current.add(jobId)
          const timer = setTimeout(() => {
            timersRef.current.delete(timer)
            cooldownRef.current.delete(jobId)
            bumpResolveTick()
          }, resolveRetryMs)
          timersRef.current.add(timer)
        })
    }
  }, [state.unresolved, state.serverTime, userId, resolveTick])

  const refreshJob = useCallback(async (jobId: string) => {
    const { getAccessToken, resolveJob = fetchChatJob } = optionsRef.current
    const accessToken = getAccessToken()
    if (!accessToken) return

    const epoch = epochRef.current
    const startedAt = eventSeqRef.current

    try {
      const envelope = await resolveJob(accessToken, jobId)

      // 그 사이 사용자가 바뀌었다면 이전 사용자의 응답은 버린다.
      if (!envelope || epochRef.current !== epoch) return
      // 조회하는 동안 같은 작업의 구독 이벤트가 왔다면 그쪽이 더 최신이라 이 응답은 버린다.
      if ((lastEventSeqRef.current.get(jobId) ?? 0) > startedAt) return

      dispatch({ type: 'event', event: { type: 'job', data: envelope } })
    } catch {
      // 다시 읽지 못해도 구독 이벤트와 재연결 snapshot이 상태를 맞춰 준다.
    }
  }, [])

  return useMemo(() => ({ ...state, refreshJob }), [state, refreshJob])
}
