import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { cancelChatJob, listConversationJobs } from './chatQueueApi'
import type { ChatAdmission, ChatJob, ChatJobEnvelope } from './chatQueueTypes'
import { isTerminalState } from './chatQueueTypes'
import { questionBlockReason } from './chatRetryRules'
import {
  ChatApiError,
  createConversation,
  listAllConversations,
  listAllMessages,
  retryQuestion,
  sendMessage,
  skipQuestion,
} from './memberChatApi'
import type { ApiConversation, ApiMessage } from './memberChatApiTypes'
import { useChatEvents, type ChatEventsView, type UseChatEventsOptions } from './useChatEvents'
import { useChatJobCommands, type ChatJobCommands } from './useChatJobCommands'

// API 명세의 질문 길이 한도다. 서버는 Unicode 코드 포인트 기준으로 세므로 UTF-16 길이(.length)를 쓰지 않는다.
export const QUESTION_MAX_CODE_POINTS = 4000

/**
 * 질문 접수·재시도·건너뛰기·취소 요청을 기다리는 최대 시간. 응답을 잃거나 서버가 늦어도 입력창과 버튼이 잠긴 채로 남지 않는다.
 * 시간이 지나면 결과를 모르는 요청으로 보고, 다시 보낼 때 같은 키로 확인한다.
 */
export const REQUEST_TIMEOUT_MS = 15_000

/** 한 대화에서 함께 읽는 작업 수. 서버 한 페이지의 최대값이다. */
const CONVERSATION_JOBS_LIMIT = 100

type PendingRequest = {
  conversationId: string
  content: string
  requestKey: string
}

/** 화면에 보일 질문 하나와 그 답변 시도들 */
export interface ChatThread {
  question: ApiMessage
  /** 작업 없이 이력에만 있는 답변(#53 이전 동기 API의 시도 등). 시도 순서대로다. */
  historyAnswers: ApiMessage[]
  /** 이 질문의 작업(첫 실행·재시도). ID 오름차순이다. */
  jobs: ChatJobEnvelope[]
}

export interface MemberChatOptions {
  /** 테스트에서 구독·작업 조회를 바꿔 끼울 때 쓴다. 기본은 실제 `GET /api/chat/events`·`GET /api/chat/jobs/{id}`다. */
  events?: Pick<UseChatEventsOptions, 'open' | 'resolveJob' | 'reconnectDelayMs' | 'resolveRetryMs'>
  requestTimeoutMs?: number
}

export interface MemberChat {
  conversations: ApiConversation[]
  conversationId: string | null
  threads: ChatThread[]
  loading: boolean
  error: string
  /** 오류가 대화를 읽지 못한 것이라 다시 불러오면 풀릴 수 있다. 전송 거절·한도 같은 오류에는 다시 불러오기를 권하지 않는다. */
  errorReloadable: boolean
  question: string
  setQuestion: (value: string) => void
  sending: boolean
  /** 같은 대화·같은 본문으로 결과를 확인하지 못한 질문이 있다. 다시 보내면 같은 request_key로 확인한다. */
  resending: boolean
  /** 새 질문을 보낼 수 없는 이유(한도·대기·간격). 같은 질문을 다시 확인할 때는 null이다. */
  blockReason: string | null
  questionLength: number
  questionTooLong: boolean
  canSubmit: boolean
  submit: () => Promise<void>
  selectConversation: (id: string) => Promise<void>
  startNewConversation: () => void
  reloadConversation: () => Promise<void>
  events: ChatEventsView
  commands: ChatJobCommands
  /** 서버 시각에 맞춘 지금 시각(ms). 남은 초·만료 시간 표시에 쓴다. */
  nowMs: number
  /** 화면을 다 그린 뒤 맨 아래로 스크롤해야 할 때 올라가는 값 */
  scrollRequest: number
}

function countCodePoints(text: string): number {
  return Array.from(text).length
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}

/** 숫자 문자열 ID를 크기순으로 비교한다. bigint ID라 Number로 바꾸지 않는다. */
export function compareIds(a: string, b: string): number {
  return a.length - b.length || (a < b ? -1 : a > b ? 1 : 0)
}

function abortable(parent: AbortSignal, timeoutMs: number) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  const timer = setTimeout(abort, timeoutMs)
  if (parent.aborted) abort()
  else parent.addEventListener('abort', abort, { once: true })

  return {
    signal: controller.signal,
    done() {
      clearTimeout(timer)
      parent.removeEventListener('abort', abort)
    },
  }
}

/** 접수 응답을 구독 상태와 같은 `{job, question, answer}` 묶음으로 바꾼다. */
function admissionEnvelope(admission: ChatAdmission): ChatJobEnvelope | null {
  return admission.job ? { job: admission.job, question: admission.question, answer: admission.answer } : null
}

/** 대화 이력의 메시지로 작업 목록의 각 작업을 `{job, question, answer}` 묶음으로 만든다. 질문을 찾지 못한 작업은 뺀다. */
function envelopesFromHistory(jobs: ChatJob[], messages: ApiMessage[]): ChatJobEnvelope[] {
  const byId = new Map(messages.map((message) => [message.id, message]))
  const envelopes: ChatJobEnvelope[] = []

  for (const job of jobs) {
    const question = byId.get(job.question_message_id)
    if (!question) continue

    const answer = job.answer_message_id ? (byId.get(job.answer_message_id) ?? null) : null
    envelopes.push({ job, question, answer })
  }

  return envelopes
}

/** 대화 이력과 구독 상태를 합쳐 질문별 답변 시도를 만든다. 같은 답변이 둘 다에 있으면 작업 쪽을 쓴다. */
export function buildThreads(
  conversationId: string | null,
  messages: ApiMessage[],
  jobs: Record<string, ChatJobEnvelope>,
): ChatThread[] {
  if (!conversationId) return []

  const questions = new Map<string, ApiMessage>()
  for (const message of messages) {
    if (message.role === 'user') questions.set(message.id, message)
  }

  const jobsByQuestion = new Map<string, ChatJobEnvelope[]>()
  const answersWithJob = new Set<string>()
  for (const envelope of Object.values(jobs)) {
    if (envelope.job.conversation_id !== conversationId) continue

    const questionId = envelope.job.question_message_id
    if (!questions.has(questionId)) questions.set(questionId, envelope.question)
    jobsByQuestion.set(questionId, [...(jobsByQuestion.get(questionId) ?? []), envelope])
    if (envelope.job.answer_message_id) answersWithJob.add(envelope.job.answer_message_id)
  }

  return [...questions.values()]
    .sort((a, b) => compareIds(a.id, b.id))
    .map((question) => ({
      question,
      historyAnswers: messages
        .filter(
          (message) =>
            message.role === 'assistant' &&
            message.reply_to_message_id === question.id &&
            !answersWithJob.has(message.id),
        )
        .sort((a, b) => (a.attempt_no ?? 0) - (b.attempt_no ?? 0)),
      jobs: (jobsByQuestion.get(question.id) ?? []).sort((a, b) => compareIds(a.job.id, b.job.id)),
    }))
}

/**
 * 회원 채팅 화면의 상태. 사이드바(대화 목록·새 대화)와 본문(질문·답변·입력창)이 같은 값을 쓴다.
 *
 * - 질문은 접수만 하고 `202`로 끝난다. 응답의 `job`·`limits`를 바로 반영하고, 그 뒤의 진행·완료는 회원 단위 구독으로 받는다.
 * - 응답을 받지 못한 질문은 키를 남겨 두고 입력창을 풀어 준다. 같은 대화·같은 본문으로 다시 보내면 같은 키로 확인한다.
 * - 회원이 바뀌면 이 상태를 쓰는 화면을 새로 마운트한다(`key={userId}`). 그래서 첫 화면부터 이전 회원의 값이 없다.
 * - 화면을 떠나면 진행 중인 요청을 끊고 늦게 온 응답은 버린다. 서버 작업은 취소하지 않는다.
 */
export function useMemberChat(options: MemberChatOptions = {}): MemberChat {
  const { accessToken, user } = useAuth()
  const userId = user?.id ?? null
  const requestTimeoutMs = options.requestTimeoutMs ?? REQUEST_TIMEOUT_MS

  const [conversations, setConversations] = useState<ApiConversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ApiMessage[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setErrorState] = useState<{ message: string; reloadable: boolean } | null>(null)
  const setError = useCallback((message: string) => {
    setErrorState(message ? { message, reloadable: false } : null)
  }, [])
  const setLoadError = useCallback((message: string) => setErrorState({ message, reloadable: true }), [])
  const [scrollRequest, setScrollRequest] = useState(0)

  // access 토큰은 약 15분마다 새 값으로 바뀐다. 토큰이 바뀔 때마다 대화를 다시 불러오면
  // 선택한 대화가 초기화되므로, 호출 시점의 최신 값을 ref로 읽고 효과는 사용자 기준으로만 건다.
  const accessTokenRef = useRef(accessToken)
  useEffect(() => {
    accessTokenRef.current = accessToken
  }, [accessToken])
  const getAccessToken = useCallback(() => accessTokenRef.current, [])

  const events = useChatEvents({ userId, getAccessToken, ...options.events })
  const eventsRef = useRef(events)
  eventsRef.current = events

  // 화면이 사라지면 진행 중인 요청을 끊는다. 늦게 온 응답을 버리는 기준이기도 하다.
  const lifetimeRef = useRef<AbortController>(new AbortController())
  // 화면에 보이는 대화의 최신 값이다. 대화를 빠르게 바꿀 때 이전 대화의 응답이 덮어쓰지 못하게 한다.
  const conversationIdRef = useRef<string | null>(null)
  // 결과를 확인하지 못한 전송 요청을 대화별로 보관한다. 같은 대화에서 같은 본문을 다시 보낼 때만 같은 키를 쓰므로,
  // 다른 대화를 다녀와도 응답을 잃은 질문의 키를 잃지 않는다.
  // 새로고침 뒤에는 서버 이력(질문의 request_key 포함)과 구독 snapshot에서 접수된 질문과 상태를 복구한다.
  const pendingRequestsRef = useRef(new Map<string, PendingRequest>())
  // 보관한 키가 바뀌면 다시 그려 입력창의 안내(같은 질문 재확인)를 맞춘다.
  const [, setPendingVersion] = useState(0)
  // 대화 조회가 겹칠 때 가장 나중에 시작한 조회의 결과만 화면에 반영하기 위한 순번이다.
  const loadSeqRef = useRef(0)
  // 진행 중인 대화 조회. 다른 대화를 열거나 새 대화를 시작하거나 화면을 떠나면 끊는다.
  const loadAbortRef = useRef<AbortController | null>(null)
  // 사용자가 목록을 읽는 동안 대화를 고르거나 새로 만들었는지 나타낸다. 그렇다면 첫 목록이 선택을 덮어쓰지 않는다.
  const navigatedRef = useRef(false)
  // 전송 중 여부를 동기적으로 막는 가드다. state는 다시 그려져야 바뀌므로, 엔터 연타나 더블클릭이 겹치면
  // 두 번째 제출이 통과해 (대화가 없을 때) 대화가 둘 만들어질 수 있다.
  const sendingRef = useRef(false)

  const alive = useCallback(() => !lifetimeRef.current.signal.aborted, [])

  useEffect(() => {
    // StrictMode의 두 번째 마운트에서도 쓸 수 있도록 마운트할 때 새로 만든다.
    if (lifetimeRef.current.signal.aborted) lifetimeRef.current = new AbortController()
    const lifetime = lifetimeRef.current
    return () => lifetime.abort()
  }, [])

  const selectId = useCallback((id: string | null) => {
    conversationIdRef.current = id
    setConversationId(id)
  }, [])

  /** 대화 하나의 이력과 작업을 함께 읽는다. 작업 목록을 읽지 못해도 이력은 보여 준다. */
  const loadConversation = useCallback(
    async (id: string) => {
      const token = accessTokenRef.current
      if (!token) return

      loadSeqRef.current += 1
      const seq = loadSeqRef.current
      const mark = eventsRef.current.mark()
      const isCurrent = () => alive() && conversationIdRef.current === id && loadSeqRef.current === seq

      // 앞서 시작한 대화 조회는 더 쓰지 않으므로 끊는다. 화면을 떠날 때도 함께 끊는다.
      loadAbortRef.current?.abort()
      const controller = new AbortController()
      loadAbortRef.current = controller
      const lifetime = lifetimeRef.current.signal
      const stop = () => controller.abort()
      if (lifetime.aborted) stop()
      else lifetime.addEventListener('abort', stop, { once: true })

      try {
        const [items, jobs] = await Promise.all([
          listAllMessages(token, id, controller.signal),
          listConversationJobs(token, id, CONVERSATION_JOBS_LIMIT, controller.signal).catch(() => null),
        ])

        if (!isCurrent()) return

        setMessages(items)
        // 재연결 snapshot에는 끝나지 않은 작업만 있으므로, 끝난 작업(재시도·건너뛰기 버튼 포함)은 이 목록으로 되살린다.
        if (jobs) {
          eventsRef.current.applyResponse(mark, {
            envelopes: envelopesFromHistory(jobs.items, items),
            limits: jobs.limits,
          })
        }
      } catch (err) {
        // 끊겼거나 더 새로운 조회가 시작됐다면 이 실패는 화면에 알리지 않는다.
        if (!isCurrent()) return
        throw err
      } finally {
        lifetime.removeEventListener('abort', stop)
        if (loadAbortRef.current === controller) loadAbortRef.current = null
      }
    },
    [alive],
  )

  useEffect(() => {
    const token = accessTokenRef.current
    if (!userId || !token) return

    let stale = false
    // 회원이 바뀌거나 화면을 떠나면 진행 중인 목록 조회를 끊는다.
    const controller = new AbortController()

    async function load(accessToken: string) {
      setLoading(true)

      try {
        const items = await listAllConversations(accessToken, controller.signal)
        if (stale) return

        // 목록을 읽는 동안 새로 만든 대화가 있으면 그대로 두고, 서버 목록에서 아직 없는 것만 뒤에 붙인다.
        setConversations((current) => [...current, ...items.filter((item) => !current.some((c) => c.id === item.id))])

        // 목록을 읽는 동안 사용자가 대화를 고르거나 새로 만들었다면 그 선택을 존중한다.
        if (navigatedRef.current) return

        const latest = items[0]

        if (latest) {
          selectId(latest.id)
          await loadConversation(latest.id)
          if (!stale) setScrollRequest((count) => count + 1)
        }
      } catch (err) {
        if (!stale) setLoadError(errorText(err, '대화 목록을 불러오지 못했습니다.'))
      } finally {
        if (!stale) setLoading(false)
      }
    }

    void load(token)

    return () => {
      stale = true
      controller.abort()
    }
  }, [userId, selectId, loadConversation, setLoadError])

  const selectConversation = async (id: string) => {
    if (!accessTokenRef.current || sendingRef.current) return

    navigatedRef.current = true
    selectId(id)
    setMessages([])
    setError('')
    setLoading(true)

    try {
      await loadConversation(id)
      if (conversationIdRef.current === id) setScrollRequest((count) => count + 1)
    } catch (err) {
      if (alive() && conversationIdRef.current === id) setLoadError(errorText(err, '메시지를 불러오지 못했습니다.'))
    } finally {
      // 그 사이 다른 대화로 바뀌었다면 로딩 표시는 새로 선택한 대화가 관리한다.
      if (alive() && conversationIdRef.current === id) setLoading(false)
    }
  }

  const reloadConversation = async () => {
    const id = conversationIdRef.current
    if (!id) return

    setError('')

    try {
      await loadConversation(id)
    } catch (err) {
      if (alive() && conversationIdRef.current === id) setLoadError(errorText(err, '대화를 다시 불러오지 못했습니다.'))
    }
  }

  const startNewConversation = () => {
    if (sendingRef.current) return

    // 보관 중인 요청 키는 지우지 않는다. 키는 대화별로 보관되어 같은 대화·같은 질문일 때만 다시 쓰이므로
    // 다른 대화로 잘못 전송되지 않고, 응답을 잃은 질문의 키도 잃지 않는다.
    navigatedRef.current = true
    loadSeqRef.current += 1
    loadAbortRef.current?.abort()
    selectId(null)
    setMessages([])
    setError('')
    setLoading(false)
  }

  // 서버 시각과 이 기기 시각의 차이. snapshot을 받을 때마다 맞춘다. 남은 초·만료 시간을 서버 기준으로 센다.
  const clockOffsetMs = useMemo(
    () => (events.serverTime ? Date.parse(events.serverTime) - Date.now() : 0),
    [events.serverTime],
  )
  const nowMs = Date.now() + clockOffsetMs
  const [, setTick] = useState(0)

  // 응답을 잃은 질문이 실제로는 접수돼 구독·이력에 나타나면 보관한 키와 입력 내용을 정리한다.
  // 그대로 두면 사용자가 문장을 고쳐 새 키로 같은 질문을 한 번 더 보낼 수 있다.
  useEffect(() => {
    let changed = false

    for (const [id, request] of pendingRequestsRef.current) {
      const isAccepted = (message: ApiMessage) => message.role === 'user' && message.request_key === request.requestKey
      const accepted =
        Object.values(events.jobs).some(
          (envelope) => envelope.job.conversation_id === id && isAccepted(envelope.question),
        ) ||
        (conversationIdRef.current === id && messages.some(isAccepted))
      if (!accepted) continue

      pendingRequestsRef.current.delete(id)
      changed = true
      if (conversationIdRef.current === id) setQuestion((current) => (current === request.content ? '' : current))
    }

    if (changed) setPendingVersion((count) => count + 1)
  }, [events.jobs, messages])

  const threads = useMemo(
    () => buildThreads(conversationId, messages, events.jobs),
    [conversationId, messages, events.jobs],
  )

  const pending = conversationId ? pendingRequestsRef.current.get(conversationId) : undefined
  const resending = !!pending && pending.content === question
  const blockReason = resending ? null : questionBlockReason(events.limits, nowMs)

  // 남은 초나 만료까지의 시간을 보여 주는 동안에만 1초마다 다시 그린다. 화면을 떠나면 타이머를 지운다.
  const needsClock =
    (!!events.limits?.next_attempt_at && Date.parse(events.limits.next_attempt_at) > nowMs) ||
    threads.some((thread) => thread.jobs.some(({ job }) => job.state === 'queued' || job.state === 'paused'))
  useEffect(() => {
    if (!needsClock) return
    const timer = setInterval(() => setTick((count) => count + 1), 1000)
    return () => clearInterval(timer)
  }, [needsClock])

  const questionLength = countCodePoints(question)
  const questionTooLong = questionLength > QUESTION_MAX_CODE_POINTS
  const canSubmit = question.trim().length > 0 && !questionTooLong && !blockReason

  const submit = async () => {
    // 서버는 질문 원문을 보존하므로 앞뒤 공백을 지우지 않고 그대로 보낸다. trim은 공백뿐인지 확인할 때만 쓴다.
    const content = question
    const token = accessTokenRef.current
    if (!canSubmit || !token || sendingRef.current) return

    sendingRef.current = true
    setSending(true)
    setError('')

    try {
      let activeId = conversationIdRef.current

      if (!activeId) {
        const request = abortable(lifetimeRef.current.signal, requestTimeoutMs)
        let conversation: ApiConversation
        try {
          conversation = await createConversation(token, request.signal)
        } finally {
          request.done()
        }
        if (!alive()) return

        activeId = conversation.id
        navigatedRef.current = true
        loadSeqRef.current += 1
        selectId(activeId)
        setConversations((previous) => [conversation, ...previous.filter((item) => item.id !== conversation.id)])
      }

      // 같은 대화에서 같은 질문을 다시 보낼 때만 이전 키를 재사용한다. 다른 대화의 보관 키는 건드리지 않는다.
      const kept = pendingRequestsRef.current.get(activeId)
      const request: PendingRequest =
        kept && kept.content === content ? kept : { conversationId: activeId, content, requestKey: crypto.randomUUID() }

      // 전송 전에 요청 키를 보관한다.
      pendingRequestsRef.current.set(activeId, request)
      setPendingVersion((count) => count + 1)

      const mark = eventsRef.current.mark()
      const call = abortable(lifetimeRef.current.signal, requestTimeoutMs)
      let admission: ChatAdmission
      try {
        admission = await sendMessage(
          accessTokenRef.current ?? token,
          request.conversationId,
          { content: request.content, request_key: request.requestKey },
          call.signal,
        )
      } finally {
        call.done()
      }

      if (!alive()) return

      // 서버가 요청을 접수했으므로 이 대화의 재전송용 키를 정리한다.
      if (pendingRequestsRef.current.get(request.conversationId) === request) {
        pendingRequestsRef.current.delete(request.conversationId)
        setPendingVersion((count) => count + 1)
      }
      setQuestion('')

      const envelope = admissionEnvelope(admission)
      eventsRef.current.applyResponse(mark, { envelopes: envelope ? [envelope] : [], limits: admission.limits })
      if (conversationIdRef.current === request.conversationId) setScrollRequest((count) => count + 1)

      // #53 이전에 저장된 질문을 같은 키로 다시 보내면 작업 없이 답변만 온다. 그때는 이력으로 보여 준다.
      if (!envelope) {
        await loadConversation(request.conversationId).catch(() => {
          if (alive()) setLoadError('질문은 접수됐지만 대화를 다시 불러오지 못했습니다. 다시 불러와 주세요.')
        })
      }
    } catch (err) {
      if (!alive()) return
      // 실패 시 보관한 키를 유지해 다음 시도에서 재사용한다.
      setError(
        err instanceof ChatApiError && err.outcomeUnknown
          ? `${err.message} 질문이 접수됐는지 확인하지 못했습니다. 그대로 다시 보내면 같은 질문으로 확인하며 중복으로 접수되지 않습니다.`
          : errorText(err, '질문 전송에 실패했습니다.'),
      )
    } finally {
      if (alive()) {
        sendingRef.current = false
        setSending(false)
      }
    }
  }

  /** 재시도·건너뛰기·취소 요청을 보낸다. 응답을 기다리는 시간에 한도를 두고, 결과를 모르면 다시 눌러 확인하게 안내한다. */
  async function command<T>(send: (token: string, signal: AbortSignal) => Promise<T>): Promise<T> {
    const token = accessTokenRef.current
    if (!token) throw new Error('로그인이 필요합니다.')

    const call = abortable(lifetimeRef.current.signal, requestTimeoutMs)
    try {
      return await send(token, call.signal)
    } catch (err) {
      if (err instanceof ChatApiError && err.outcomeUnknown) {
        throw new ChatApiError(
          `${err.message} 다시 누르면 같은 요청으로 확인합니다.`,
          err.status,
          err.code,
          err.retryAfterSeconds,
        )
      }
      throw err
    } finally {
      call.done()
    }
  }

  function envelopeOf(jobId: string): ChatJobEnvelope {
    const envelope = eventsRef.current.jobs[jobId]
    if (!envelope) throw new Error('이 작업을 찾지 못했습니다. 대화를 다시 불러와 주세요.')
    return envelope
  }

  /** 응답으로 받은 작업을 이미 알고 있는 질문·답변과 묶는다. 처음 보는 작업은 구독 이벤트를 기다린다. */
  function withKnown(jobs: ChatJob[]): ChatJobEnvelope[] {
    const known = eventsRef.current.jobs
    return jobs.flatMap((job) => {
      const envelope = known[job.id]
      if (!envelope) return []
      // 끝난 작업이 다시 진행 중으로 돌아가지는 않는다. 응답이 늦게 왔다면 알고 있는 값을 둔다.
      if (isTerminalState(envelope.job.state) && envelope.job.state !== job.state) return []
      return [{ ...envelope, job }]
    })
  }

  const commands = useChatJobCommands(
    userId,
    {
      retry: async (jobId, retryKey) => {
        const { job } = envelopeOf(jobId)
        const mark = eventsRef.current.mark()
        const admission = await command((token, signal) =>
          retryQuestion(token, job.conversation_id, job.question_message_id, retryKey, signal),
        )
        if (!alive()) return
        const envelope = admissionEnvelope(admission)
        eventsRef.current.applyResponse(mark, { envelopes: envelope ? [envelope] : [], limits: admission.limits })
      },
      skip: async (jobId) => {
        const { job } = envelopeOf(jobId)
        const mark = eventsRef.current.mark()
        const result = await command((token, signal) =>
          skipQuestion(token, job.conversation_id, job.question_message_id, signal),
        )
        if (!alive()) return
        eventsRef.current.applyResponse(mark, { envelopes: withKnown(result.resumed_jobs), limits: result.limits })
        // 건너뛴 실패 작업의 버튼(`["retry"]`)은 응답에 없으므로 다시 읽는다. 구독 이벤트가 먼저 오면 그쪽을 쓴다.
        void eventsRef.current.refreshJob(jobId)
      },
      cancel: async (jobId) => {
        const mark = eventsRef.current.mark()
        const result = await command((token, signal) => cancelChatJob(token, jobId, signal))
        if (!alive()) return
        eventsRef.current.applyResponse(mark, { envelopes: withKnown([result.job]), limits: result.limits })
      },
    },
    // 서버가 거절했다면 화면이 뒤처졌을 수 있으니 그 작업을 다시 읽는다.
    { onFailure: (jobId) => eventsRef.current.refreshJob(jobId) },
  )

  return {
    conversations,
    conversationId,
    threads,
    loading,
    error: error?.message ?? '',
    errorReloadable: error?.reloadable ?? false,
    question,
    setQuestion,
    sending,
    resending,
    blockReason,
    questionLength,
    questionTooLong,
    canSubmit,
    submit,
    selectConversation,
    startNewConversation,
    reloadConversation,
    events,
    commands,
    nowMs,
    scrollRequest,
  }
}
