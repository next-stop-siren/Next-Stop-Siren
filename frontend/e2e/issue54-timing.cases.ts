import { expect, type Page, test } from '@playwright/test'
import {
  ORIGIN,
  ageJob,
  allowNext,
  configureProvider,
  controlChatEvents,
  databaseState,
  dropFirstQuestionResponse,
  exhaustQuota,
  memberState,
  openMemberChat,
  releaseGate,
  setChatEventsBlocked,
  signUp,
  submit,
  thread,
  uniqueQuestion,
  waitForGate,
  saveEvidence,
  type MemberState,
  type SubmitEnvelope,
} from './issue54.helpers'

const CLOCK_SKEW_MS = 4 * 60 * 1000

type Job = {
  id: string
  state: string
  kind: 'question' | 'retry'
  conversation_id: string
  accepted_at?: string
  expires_at: string
  finished_at?: string | null
}

type JobList = {
  items: Job[]
  limits: MemberState['limits']
  server_time: string
}

type JobEnvelope = {
  job: Job
  question: { id: string; content: string; request_key: string }
  answer: { id: string; status: string; content: string | null } | null
}

type PostedQuestion = { request_key: string; content: string }

function captureBearer(page: Page) {
  let bearer = ''
  page.on('request', (request) => {
    if (new URL(request.url()).pathname !== '/api/chat/events') return
    const value = request.headers().authorization
    if (value?.startsWith('Bearer ')) bearer = value
  })
  return () => bearer
}

async function apiGet<T>(page: Page, path: string, bearer: string): Promise<T> {
  expect(bearer).toMatch(/^Bearer\s+\S+/)
  const response = await page.request.get(new URL(path, ORIGIN).toString(), {
    headers: { Authorization: bearer },
  })
  expect(response.status()).toBe(200)
  return (await response.json()) as T
}

async function jobsFor(page: Page, conversationId: string, bearer: string) {
  return apiGet<JobList>(page, `/api/chat/jobs?conversation_id=${conversationId}`, bearer)
}

function skewClock(page: Page) {
  return page.addInitScript((offset) => {
    const actualNow = Date.now
    Date.now = () => actualNow() + offset
  }, CLOCK_SKEW_MS)
}

function kstTime(iso: string) {
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(new Date(iso))
}

function secondsFrom(text: string, pattern: RegExp) {
  const match = pattern.exec(text)
  expect(match).not.toBeNull()
  return Number(match?.[1])
}

async function askWithoutWaitingForResponse(page: Page, question: string) {
  await page.getByLabel('질문 입력').fill(question)
  await page.getByRole('button', { name: '질문 보내기' }).click()
}

test('B2: POST 응답을 잃어도 연결된 SSE로 기존 request_key 작업을 복구한다', async ({ page }) => {
  const email = await signUp(page, 'issue54-b2-sse')
  await openMemberChat(page)

  const question = uniqueQuestion('script')
  configureProvider(question, ['hold'])
  const posted = (await dropFirstQuestionResponse(page)) as PostedQuestion[]

  await askWithoutWaitingForResponse(page, question)
  await expect(page.getByRole('alert')).toContainText('질문이 접수됐는지 확인하지 못했습니다')
  await waitForGate(question, 'attempt_1')
  await expect(page.getByLabel('질문 입력')).toBeEnabled()
  await expect(thread(page, question)).toContainText('답변 생성 중입니다.')
  expect(posted).toHaveLength(1)
  expect(posted[0]?.request_key).toBeTruthy()

  expect(await databaseState(question)).toMatchObject({
    job_state: 'running',
    answer_status: 'pending',
    answer_content: null,
    reservation_state: 'reserved',
  })
  const before = await memberState(email)
  expect(before.question_count).toBe(1)
  expect(before.reservation_count).toBe(1)

  releaseGate(question, 'attempt_1')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  expect(posted).toHaveLength(1)
  const after = await memberState(email)
  expect(after.question_count).toBe(1)
  expect(after.reservation_count).toBe(1)
  await saveEvidence(email, question, before.items[0]!.job_id)
})

test('B2: SSE를 abort한 뒤 10초 안에 한도 0에서도 같은 request_key로 재조회한다', async ({ page }) => {
  await controlChatEvents(page)
  const email = await signUp(page, 'issue54-b2-offline')
  const bearer = captureBearer(page)
  await openMemberChat(page)

  await setChatEventsBlocked(page, true)
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 끊김, 다시 연결 중')

  const question = uniqueQuestion('script')
  configureProvider(question, ['hold'])
  const posted = (await dropFirstQuestionResponse(page)) as PostedQuestion[]
  await askWithoutWaitingForResponse(page, question)
  await expect(page.getByRole('alert')).toContainText('질문이 접수됐는지 확인하지 못했습니다')
  await waitForGate(question, 'attempt_1')
  await expect(page.getByRole('button', { name: '다시 확인하기' })).toBeEnabled()
  expect(posted).toHaveLength(1)
  expect(posted[0]?.request_key).toBeTruthy()

  const initial = await memberState(email)
  expect(initial.question_count).toBe(1)
  const conversationId = initial.items[0]!.conversation_id
  const firstJobId = initial.items[0]!.job_id

  await exhaustQuota(email)
  const beforeJobs = await jobsFor(page, conversationId, bearer())
  const beforeLimits = beforeJobs.limits
  expect(beforeLimits.daily.remaining).toBeGreaterThan(0)
  expect(beforeLimits.six_hour.remaining).toBe(0)
  expect(beforeLimits.next_attempt_at).not.toBeNull()
  const waitMs = Date.parse(beforeLimits.next_attempt_at!) - Date.parse(beforeJobs.server_time)
  expect(waitMs).toBeGreaterThan(0)
  expect(waitMs).toBeLessThanOrEqual(10_000)
  const beforeCounts = await memberState(email)

  const resend = page.waitForResponse((response) => {
    const request = response.request()
    return request.method() === 'POST' && new URL(response.url()).pathname.endsWith('/messages')
  })
  await page.getByRole('button', { name: '다시 확인하기' }).click()
  const resendResponse = await resend
  expect(resendResponse.status()).toBe(202)
  const resent = (await resendResponse.json()) as SubmitEnvelope
  expect(resent.job.id).toBe(firstJobId)
  expect(posted).toHaveLength(2)
  expect(posted[1]?.request_key).toBe(posted[0]?.request_key)
  await expect(page.getByLabel('질문 입력')).toHaveValue('')

  const afterCounts = await memberState(email)
  expect(afterCounts.question_count).toBe(beforeCounts.question_count)
  expect(afterCounts.reservation_count).toBe(beforeCounts.reservation_count)
  expect((await databaseState(question)).job_id).toBe(firstJobId)

  await setChatEventsBlocked(page, false)
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
  releaseGate(question, 'attempt_1')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  await saveEvidence(email, question, firstJobId)
})

test('B9: 4분 빠른 클라이언트 시계에서도 실제 서버 간격·한도 초기화 시각을 표시한다', async ({ page }) => {
  await skewClock(page)
  const email = await signUp(page, 'issue54-b9-clock')
  const bearer = captureBearer(page)
  await openMemberChat(page)

  const browserSkew = (await page.evaluate(() => Date.now())) - Date.now()
  expect(browserSkew).toBeGreaterThanOrEqual(CLOCK_SKEW_MS - 2_000)
  expect(browserSkew).toBeLessThanOrEqual(CLOCK_SKEW_MS + 2_000)

  await allowNext(email)
  const question = uniqueQuestion('script')
  const accepted = await submit(page, question)
  expect(accepted.limits.next_attempt_at).not.toBeNull()
  const conversationId = accepted.job.conversation_id
  const firstJobs = await jobsFor(page, conversationId, bearer())
  const expectedSeconds = Math.ceil(
    (Date.parse(accepted.limits.next_attempt_at!) - Date.parse(firstJobs.server_time)) / 1_000,
  )
  const hint = page.locator('#chat-question-count')
  await expect(hint).toContainText(/초 뒤에 보낼 수 있습니다/)
  const shownSeconds = secondsFrom(await hint.innerText(), /(\d+)초 뒤에 보낼 수 있습니다/)
  expect(Math.abs(shownSeconds - expectedSeconds)).toBeLessThanOrEqual(2)

  await page.waitForTimeout(1_100)
  await expect
    .poll(async () => secondsFrom(await hint.innerText(), /(\d+)초 뒤에 보낼 수 있습니다/), { timeout: 2_500 })
    .toBeLessThan(shownSeconds)

  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  await exhaustQuota(email)
  const actualLimits = await apiGet<MemberState['limits']>(page, '/api/chat/limits', bearer())
  expect(actualLimits.daily.remaining).toBeGreaterThan(0)
  expect(actualLimits.six_hour.remaining).toBe(0)
  await page.reload()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')

  const latestReset = [actualLimits.daily, actualLimits.six_hour]
    .filter((window) => window.remaining <= 0)
    .map((window) => window.resets_at)
    .sort()
    .at(-1)!
  const expectedResetLabel = `다음 초기화: ${kstTime(latestReset)}`
  await expect(page.getByText(/오늘 남은 질문 300\/400/)).toBeVisible()
  await expect(page.locator('#chat-question-count')).toContainText(expectedResetLabel)
  await expect(page.getByRole('button', { name: '질문 보내기' })).toBeDisabled()

  const afterReloadJobs = await jobsFor(page, conversationId, bearer())
  const apiJob = await apiGet<JobEnvelope>(page, `/api/chat/jobs/${accepted.job.id}`, bearer())
  expect(apiJob.job.state).toBe('completed')
  expect(afterReloadJobs.limits.daily.resets_at).toBe(actualLimits.daily.resets_at)
  expect(afterReloadJobs.limits.six_hour.resets_at).toBe(actualLimits.six_hour.resets_at)
  await saveEvidence(email, question, accepted.job.id)
})

test('B9: 실제 expires_at 기준으로 queued 만료 초를 안내하고 worker 만료 후 회수한다', async ({ page }) => {
  test.setTimeout(45_000)
  await skewClock(page)
  const email = await signUp(page, 'issue54-b9-expiry')
  const bearer = captureBearer(page)
  await openMemberChat(page)

  const runningQuestion = uniqueQuestion('script')
  configureProvider(runningQuestion, ['hold'])
  const running = await submit(page, runningQuestion)
  await waitForGate(runningQuestion, 'attempt_1')
  let released = false
  try {
    expect(await databaseState(runningQuestion)).toMatchObject({
      job_state: 'running',
      answer_status: 'pending',
      reservation_state: 'reserved',
    })

    await allowNext(email)
    const waitingQuestion = uniqueQuestion('script')
    const waiting = await submit(page, waitingQuestion)
    expect(waiting.job.state).toBe('queued')
    const waitingJobId = waiting.job.id
    const initialJobs = await jobsFor(page, waiting.job.conversation_id, bearer())
    const initialJob = await apiGet<JobEnvelope>(page, `/api/chat/jobs/${waitingJobId}`, bearer())
    const initialApiSeconds = Math.ceil(
      (Date.parse(initialJob.job.expires_at) - Date.parse(initialJobs.server_time)) / 1_000,
    )
    const card = page.locator(`[data-job-id="${waitingJobId}"]`)
    const initialMatch = /만료까지 (?:(\d+)분 )?(\d+)초/.exec(await card.innerText())!
    const initialTotalSeconds = Number(initialMatch[1] ?? 0) * 60 + Number(initialMatch[2])
    expect(Math.abs(initialTotalSeconds - initialApiSeconds)).toBeLessThanOrEqual(2)
    expect(initialTotalSeconds).toBeGreaterThan(0)

    await ageJob(waitingQuestion, 296)
    const observed = await jobsFor(page, waiting.job.conversation_id, bearer())
    const actualJob = await apiGet<JobEnvelope>(page, `/api/chat/jobs/${waitingJobId}`, bearer())
    expect(observed.server_time).toMatch(/^\d{4}-\d\d-\d\dT/)
    expect(actualJob.job.expires_at).toBeTruthy()
    const apiRemaining = Math.ceil((Date.parse(actualJob.job.expires_at) - Date.parse(observed.server_time)) / 1_000)
    expect(apiRemaining).toBeGreaterThan(0)
    expect(apiRemaining).toBeLessThanOrEqual(6)

    await page.reload()
    await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
    await expect(card).toContainText(/만료까지 \d+초/)
    const shownSeconds = secondsFrom(await card.innerText(), /만료까지 (\d+)초/)
    expect(Math.abs(shownSeconds - apiRemaining)).toBeLessThanOrEqual(2)

    await page.waitForTimeout(Math.max(0, Date.parse(actualJob.job.expires_at) - Date.now() + 100))
    const atDeadlineJobs = await jobsFor(page, waiting.job.conversation_id, bearer())
    const atDeadline = atDeadlineJobs.items.find((job) => job.id === waitingJobId)
    expect(atDeadline).toBeDefined()
    expect(['queued', 'expired']).toContain(atDeadline?.state)
    const gateReleasedAt = new Date().toISOString()

    releaseGate(runningQuestion, 'attempt_1')
    released = true
    await expect
      .poll(async () => (await apiGet<JobEnvelope>(page, `/api/chat/jobs/${waitingJobId}`, bearer())).job.state, {
        timeout: 9_000,
      })
      .toBe('expired')
    const expired = await apiGet<JobEnvelope>(page, `/api/chat/jobs/${waitingJobId}`, bearer())
    expect(expired.job.state).toBe('expired')
    expect(expired.job.finished_at).toBeTruthy()
    const afterWorker = await jobsFor(page, waiting.job.conversation_id, bearer())
    const overrunMs = Date.parse(expired.job.finished_at!) - Date.parse(actualJob.job.expires_at)
    expect(overrunMs).toBeGreaterThanOrEqual(0)
    process.stdout.write(
      `ISSUE54_B9_EXPIRY ${JSON.stringify({
        job_id: waitingJobId,
        accepted_at_before_age: initialJob.job.accepted_at,
        accepted_at_after_age: actualJob.job.accepted_at,
        expires_at: actualJob.job.expires_at,
        server_time_at_ui_check: observed.server_time,
        seconds_at_ui_check: apiRemaining,
        server_time_at_deadline: atDeadlineJobs.server_time,
        job_state_at_deadline: atDeadline?.state,
        gate_released_at: gateReleasedAt,
        finished_at_after_worker_claim: expired.job.finished_at,
        server_time_after_worker_claim: afterWorker.server_time,
        overrun_ms: overrunMs,
      })}\n`,
    )
    await expect(card).toContainText('대기 시간이 지나 취소됨')
    expect(await databaseState(waitingQuestion)).toMatchObject({
      job_state: 'expired',
      reservation_state: 'refunded',
      refund_reason: 'expired',
    })
    await saveEvidence(email, waitingQuestion, waitingJobId)
  } finally {
    if (!released) releaseGate(runningQuestion, 'attempt_1')
  }

  await expect(thread(page, runningQuestion)).toContainText('답변 완료 · 저장됨')
  await saveEvidence(email, runningQuestion, running.job.id)
  expect(released).toBe(true)
})
