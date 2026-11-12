import { expect, type Browser, type Page, type Route, test } from '@playwright/test'
import {
  ORIGIN,
  PASSWORD,
  ageJob,
  allowNext,
  configureProvider,
  controlChatEvents,
  databaseState,
  memberState,
  openMemberChat,
  releaseGate,
  saveEvidence,
  setChatEventsBlocked,
  signIn,
  signUp,
  submit,
  thread,
  uniqueQuestion,
  waitForGate,
  type ProviderOutcome,
  type SubmitEnvelope,
} from './issue54.helpers'

type JobView = {
  id: string
  kind: 'question' | 'retry'
  state: string
  conversation_id: string
  question_message_id: string
  paused_by_message_id: string | null
  allowed_actions: string[]
}

async function accessToken(page: Page, email: string) {
  const response = await page.request.post(`${ORIGIN}/api/auth/login`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(200)
  return (await response.json()).access_token as string
}

async function readJob(page: Page, token: string, jobId: string) {
  const response = await page.request.get(`${ORIGIN}/api/chat/jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.status()).toBe(200)
  return (await response.json()) as { job: JobView }
}

function jobCard(page: Page, question: string, jobId: string) {
  return thread(page, question).locator(`[data-job-id="${jobId}"]`)
}

async function expectActions(page: Page, token: string, question: string, jobId: string) {
  const { job } = await readJob(page, token, jobId)
  const card = jobCard(page, question, jobId)
  const labels: Record<string, string> = {
    retry: '재시도',
    skip: '건너뛰기',
    cancel: job.kind === 'retry' ? '재시도 취소' : '질문 취소',
  }
  for (const [action, label] of Object.entries(labels)) {
    await expect(card.getByRole('button', { name: label, exact: true })).toHaveCount(
      job.allowed_actions.includes(action) ? 1 : 0,
    )
  }
  return job
}

async function selectConversation(page: Page, token: string, conversationId: string, question: string) {
  const response = await page.request.get(`${ORIGIN}/api/conversations?limit=20`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.status()).toBe(200)
  const body = (await response.json()) as { items: { id: string }[] }
  const index = body.items.findIndex((conversation) => conversation.id === conversationId)
  expect(index).toBeGreaterThanOrEqual(0)
  await page.getByRole('navigation', { name: '대화 기록' }).getByRole('button').nth(index).click()
  await expect(thread(page, question)).toBeVisible()
}

async function newConversation(page: Page) {
  await page.getByRole('button', { name: '＋ 새 대화' }).click()
  await expect(page.getByLabel('질문 입력')).toBeVisible()
}

async function reopenAdmission(page: Page, email: string) {
  await allowNext(email)
  await page.reload()
  await openMemberChat(page)
}

async function waitForState(question: string, state: string) {
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 10_000 }).toBe(state)
}

async function retryFromUi(page: Page, question: string, failedJobId: string): Promise<SubmitEnvelope> {
  const responsePromise = page.waitForResponse((response) => {
    const request = response.request()
    return (
      request.method() === 'POST' &&
      /\/api\/conversations\/\d+\/messages\/\d+\/retry$/.test(new URL(response.url()).pathname)
    )
  })
  await jobCard(page, question, failedJobId).getByRole('button', { name: '재시도', exact: true }).click()
  const response = await responsePromise
  expect(response.status()).toBe(202)
  return (await response.json()) as SubmitEnvelope
}

async function submitAndReadStatus(page: Page, question: string) {
  const responsePromise = page.waitForResponse((response) => {
    const request = response.request()
    return request.method() === 'POST' && /\/api\/conversations\/\d+\/messages$/.test(new URL(response.url()).pathname)
  })
  await page.getByLabel('질문 입력').fill(question)
  await page.getByRole('button', { name: '질문 보내기' }).click()
  const response = await responsePromise
  return { status: response.status(), body: await response.json() }
}

async function waitForAnswer(question: string, status: string, reservationState: string) {
  await expect
    .poll(
      async () => {
        const state = await databaseState(question)
        return [state.job_state, state.answer_status, state.reservation_state]
      },
      { timeout: 12_000 },
    )
    .toEqual([status, status, reservationState])
}

test('B7: 재시도 성공은 같은 대화의 정지를 풀고, 다른 대화는 먼저 진행한다', async ({ page }) => {
  const email = await signUp(page, 'b7-retry-success')
  await openMemberChat(page)
  const token = await accessToken(page, email)
  const failedQuestion = uniqueQuestion('script')
  const followerQuestion = uniqueQuestion('script')
  const otherQuestion = uniqueQuestion('script')
  configureProvider(failedQuestion, ['fail', 'success'])
  configureProvider(followerQuestion, ['hold'])
  configureProvider(otherQuestion, ['success'])

  const failed = await submit(page, failedQuestion)
  await waitForState(failedQuestion, 'failed')
  await expect(thread(page, failedQuestion)).toContainText('답변을 만들지 못했습니다.')
  const failedActions = await expectActions(page, token, failedQuestion, failed.job.id)
  expect(failedActions.allowed_actions).toEqual(['retry', 'skip'])

  await reopenAdmission(page, email)
  await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
  const follower = await submit(page, followerQuestion)
  expect(follower.job.state).toBe('paused')
  const pausedActions = await expectActions(page, token, followerQuestion, follower.job.id)
  expect(pausedActions.allowed_actions).toEqual(['cancel'])
  expect(pausedActions.paused_by_message_id).toBe(failed.question.id)
  expect((await databaseState(followerQuestion)).reservation_state).toBe('reserved')

  await reopenAdmission(page, email)
  await newConversation(page)
  const other = await submit(page, otherQuestion)
  await waitForAnswer(otherQuestion, 'completed', 'settled')
  await saveEvidence(email, otherQuestion, other.job.id)

  await reopenAdmission(page, email)
  await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
  const retry = await retryFromUi(page, failedQuestion, failed.job.id)
  await waitForAnswer(failedQuestion, 'completed', 'settled')
  expect((await databaseState(failedQuestion)).job_id).toBe(retry.job.id)
  await waitForGate(followerQuestion, 'attempt_1')
  const runningFollower = await databaseState(followerQuestion)
  expect(runningFollower).toMatchObject({
    job_state: 'running',
    answer_status: 'pending',
    reservation_state: 'reserved',
  })
  const runningActions = await expectActions(page, token, followerQuestion, follower.job.id)
  expect(runningActions.allowed_actions).toEqual([])
  await saveEvidence(email, failedQuestion, failed.job.id)

  try {
    releaseGate(followerQuestion, 'attempt_1')
    await waitForAnswer(followerQuestion, 'completed', 'settled')
    await saveEvidence(email, followerQuestion, follower.job.id)
  } finally {
    releaseGate(followerQuestion, 'attempt_1')
  }
})

test('B7: 유효한 건너뛰기만 후속 질문을 재개하고 대기 취소만 허용한다', async ({ page }) => {
  const email = await signUp(page, 'b7-skip-cancel')
  await openMemberChat(page)
  const token = await accessToken(page, email)
  const failedQuestion = uniqueQuestion('script')
  const followerQuestion = uniqueQuestion('script')
  const canceledQuestion = uniqueQuestion('script')
  configureProvider(failedQuestion, ['fail'])
  configureProvider(followerQuestion, ['hold'])
  configureProvider(canceledQuestion, ['hold'])

  const failed = await submit(page, failedQuestion)
  await waitForState(failedQuestion, 'failed')
  const follower = await (async () => {
    await reopenAdmission(page, email)
    await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
    return submit(page, followerQuestion)
  })()
  expect(follower.job.state).toBe('paused')
  const followerReservation = (await databaseState(followerQuestion)).reservation_id

  const skipResponse = page.waitForResponse((response) => {
    const request = response.request()
    return (
      request.method() === 'POST' &&
      /\/api\/conversations\/\d+\/messages\/\d+\/skip$/.test(new URL(response.url()).pathname)
    )
  })
  await jobCard(page, failedQuestion, failed.job.id).getByRole('button', { name: '건너뛰기', exact: true }).click()
  expect((await skipResponse).status()).toBe(200)
  await waitForGate(followerQuestion, 'attempt_1')
  const skippedActions = await expectActions(page, token, failedQuestion, failed.job.id)
  expect(skippedActions.allowed_actions).toEqual([])
  expect((await databaseState(followerQuestion)).job_state).toBe('running')
  const runningFollower = await expectActions(page, token, followerQuestion, follower.job.id)
  expect(runningFollower.allowed_actions).toEqual([])
  await expect(jobCard(page, followerQuestion, follower.job.id).getByRole('button', { name: '질문 취소' })).toHaveCount(
    0,
  )

  await reopenAdmission(page, email)
  await newConversation(page)
  const canceled = await submit(page, canceledQuestion)
  expect(canceled.job.state).toBe('queued')
  const queuedActions = await expectActions(page, token, canceledQuestion, canceled.job.id)
  expect(queuedActions.allowed_actions).toEqual(['cancel'])
  const cancelResponse = page.waitForResponse((response) => {
    const request = response.request()
    return (
      request.method() === 'POST' &&
      new URL(response.url()).pathname.endsWith(`/api/chat/jobs/${canceled.job.id}/cancel`)
    )
  })
  await jobCard(page, canceledQuestion, canceled.job.id).getByRole('button', { name: '질문 취소', exact: true }).click()
  expect((await cancelResponse).status()).toBe(200)
  expect(await databaseState(canceledQuestion)).toMatchObject({
    job_state: 'canceled',
    reservation_state: 'refunded',
    refund_reason: 'canceled',
  })
  expect((await databaseState(followerQuestion)).reservation_id).toBe(followerReservation)
  expect((await databaseState(followerQuestion)).reservation_state).toBe('reserved')
  await expect(jobCard(page, canceledQuestion, canceled.job.id).getByRole('button', { name: '질문 취소' })).toHaveCount(
    0,
  )

  try {
    releaseGate(followerQuestion, 'attempt_1')
    await waitForAnswer(followerQuestion, 'completed', 'settled')
    await saveEvidence(email, failedQuestion, failed.job.id)
    await saveEvidence(email, followerQuestion, follower.job.id)
    await saveEvidence(email, canceledQuestion, canceled.job.id)
  } finally {
    releaseGate(followerQuestion, 'attempt_1')
  }
})

const retryEndings: {
  name: string
  outcome: ProviderOutcome
  action: 'finish' | 'cancel' | 'expire'
  finalState: string
}[] = [
  { name: '실패', outcome: 'fail', action: 'finish', finalState: 'failed' },
  { name: '중단', outcome: 'interrupted', action: 'finish', finalState: 'interrupted' },
  { name: '취소', outcome: 'success', action: 'cancel', finalState: 'canceled' },
  { name: '만료', outcome: 'success', action: 'expire', finalState: 'expired' },
]

for (const ending of retryEndings) {
  test(`B7: 재시도 ${ending.name} 뒤에도 같은 대화 후속은 정지 상태로 남는다`, async ({ page }) => {
    const email = await signUp(page, `b7-retry-${ending.action}-${ending.finalState}`)
    await openMemberChat(page)
    const token = await accessToken(page, email)
    const failedQuestion = uniqueQuestion('script')
    const followerQuestion = uniqueQuestion('script')
    const blockerQuestion = uniqueQuestion('script')
    configureProvider(failedQuestion, ['fail', ending.outcome])
    configureProvider(followerQuestion, ['hold'])
    configureProvider(blockerQuestion, ['hold'])

    const failed = await submit(page, failedQuestion)
    await waitForState(failedQuestion, 'failed')
    await reopenAdmission(page, email)
    await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
    const follower = await submit(page, followerQuestion)
    expect(follower.job.state).toBe('paused')

    await reopenAdmission(page, email)
    await newConversation(page)
    const blocker = await submit(page, blockerQuestion)
    await waitForGate(blockerQuestion, 'attempt_1')
    const gates: [string, `attempt_${number}`][] = [[blockerQuestion, 'attempt_1']]

    try {
      await reopenAdmission(page, email)
      await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
      const retry = await retryFromUi(page, failedQuestion, failed.job.id)
      expect(retry.job.kind).toBe('retry')
      expect(retry.job.state).toBe('queued')
      const retryActions = await expectActions(page, token, failedQuestion, retry.job.id)
      expect(retryActions.allowed_actions).toEqual(['cancel'])
      expect((await memberState(email)).limits.queue).toMatchObject({
        running: 1,
        questions_waiting: 1,
        retries_waiting: 1,
      })

      if (ending.action === 'cancel') {
        const cancelResponse = page.waitForResponse((response) => {
          const request = response.request()
          return (
            request.method() === 'POST' &&
            new URL(response.url()).pathname.endsWith(`/api/chat/jobs/${retry.job.id}/cancel`)
          )
        })
        await jobCard(page, failedQuestion, retry.job.id)
          .getByRole('button', { name: '재시도 취소', exact: true })
          .click()
        expect((await cancelResponse).status()).toBe(200)
      } else if (ending.action === 'expire') {
        await ageJob(failedQuestion, 301)
      }

      if (ending.action === 'finish') {
        releaseGate(blockerQuestion, 'attempt_1')
        await waitForAnswer(failedQuestion, ending.finalState, 'refunded')
        const finalRetry = await databaseState(failedQuestion)
        expect(finalRetry.refund_reason).toBe(ending.finalState === 'failed' ? 'failed' : 'interrupted')
        await expect(thread(page, failedQuestion)).toContainText(
          ending.finalState === 'failed' ? '답변을 만들지 못했습니다.' : '답변 생성이 중단되었습니다.',
        )
        const retryTerminal = await expectActions(page, token, failedQuestion, retry.job.id)
        expect(retryTerminal.allowed_actions).toEqual(['retry', 'skip'])
      } else {
        releaseGate(blockerQuestion, 'attempt_1')
        await waitForAnswer(blockerQuestion, 'completed', 'settled')
        await waitForState(failedQuestion, ending.finalState)
        const finalRetry = await databaseState(failedQuestion)
        expect(finalRetry.reservation_state).toBe('refunded')
        expect(finalRetry.refund_reason).toBe(ending.finalState)
        expect((await databaseState(followerQuestion)).job_state).toBe('paused')
        expect((await databaseState(followerQuestion)).reservation_state).toBe('reserved')
        await expect(jobCard(page, failedQuestion, retry.job.id)).toContainText(
          ending.finalState === 'canceled' ? '취소한 질문' : '대기 시간이 지나 취소됨',
        )
        const originalActions = await expectActions(page, token, failedQuestion, failed.job.id)
        expect(originalActions.allowed_actions).toEqual(['retry', 'skip'])
        const finalRetryActions = await expectActions(page, token, failedQuestion, retry.job.id)
        expect(finalRetryActions.allowed_actions).toEqual([])
        await expect(thread(page, followerQuestion)).toContainText('앞 질문 실패로 대기 중')
      }

      await saveEvidence(email, failedQuestion, failed.job.id)
      await saveEvidence(email, followerQuestion, follower.job.id)
      await saveEvidence(email, blockerQuestion, blocker.job.id)
      await saveEvidence(email, failedQuestion, retry.job.id)
    } finally {
      for (const [question, stage] of gates) releaseGate(question, stage)
      releaseGate(blockerQuestion, 'attempt_1')
      releaseGate(failedQuestion, 'attempt_2')
      releaseGate(followerQuestion, 'attempt_1')
    }
  })
}

test('B2/B7: retry 응답을 잃은 뒤 재시도 키를 재사용해 예약을 중복하지 않는다', async ({ page }) => {
  await controlChatEvents(page)
  const email = await signUp(page, 'b2-retry-response-lost')
  await openMemberChat(page)
  const token = await accessToken(page, email)
  const question = uniqueQuestion('script')
  configureProvider(question, ['fail', 'hold'])
  const failed = await submit(page, question)
  await waitForState(question, 'failed')
  await expectActions(page, token, question, failed.job.id)
  await page.evaluate(() => {
    const control = window as unknown as { __blockChatEvents: boolean; __cutChatEvents: () => void }
    control.__blockChatEvents = true
    control.__cutChatEvents()
  })

  const keys: string[] = []
  let originalResponseJobId = ''
  let authorization = ''
  const retryRoute = async (route: Route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = JSON.parse(route.request().postData() ?? '{}') as { retry_key: string }
    keys.push(body.retry_key)
    authorization = (await route.request().headerValue('authorization')) ?? ''
    if (keys.length === 1) {
      const response = await route.fetch()
      expect(response.status()).toBe(202)
      originalResponseJobId = ((await response.json()) as SubmitEnvelope).job.id
      await route.abort('failed')
      return
    }
    return route.fallback()
  }
  await page.route('**/api/conversations/*/messages/*/retry', retryRoute)

  try {
    const retryCard = jobCard(page, question, failed.job.id)
    await retryCard.getByRole('button', { name: '재시도', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('다시 누르면 같은 요청으로 확인합니다.')
    await waitForGate(question, 'attempt_2')
    const state = await databaseState(question)
    expect(state).toMatchObject({ job_id: originalResponseJobId, job_state: 'running', reservation_state: 'reserved' })
    expect((await readJob(page, token, failed.job.id)).job.allowed_actions).toEqual([])
    await expect(page.getByRole('alert')).toContainText('다시 누르면 같은 요청으로 확인합니다.')

    const retryAgain = retryCard.getByRole('button', { name: '재시도', exact: true })
    const retryActionVisible = await retryAgain.isVisible()
    if (!retryActionVisible) {
      // Confirm the precise lost-response state before classifying the UI recovery gap.
      await expect(retryAgain).toHaveCount(0)
      await expect(jobCard(page, question, originalResponseJobId)).toHaveCount(0)
      const response = await page.request.post(
        `${ORIGIN}/api/conversations/${failed.job.conversation_id}/messages/${failed.question.id}/retry`,
        { headers: { Authorization: authorization, Origin: ORIGIN }, data: { retry_key: keys[0] } },
      )
      expect(response.status()).toBe(202)
      expect(((await response.json()) as SubmitEnvelope).job.id).toBe(originalResponseJobId)
      expect(keys).toHaveLength(1)
      const member = await memberState(email)
      expect(member.items.filter((item) => item.job_kind === 'retry')).toHaveLength(1)
      expect(member.reservation_count).toBe(2)
    } else {
      const secondResponse = page.waitForResponse((response) => {
        const request = response.request()
        return (
          request.method() === 'POST' &&
          /\/api\/conversations\/\d+\/messages\/\d+\/retry$/.test(new URL(response.url()).pathname)
        )
      })
      await retryAgain.click()
      const response = await secondResponse
      expect(response.status()).toBe(202)
      expect(keys).toHaveLength(2)
      expect(keys[1]).toBe(keys[0])
      expect(((await response.json()) as SubmitEnvelope).job.id).toBe(originalResponseJobId)
      expect((await memberState(email)).reservation_count).toBe(2)
    }
    releaseGate(question, 'attempt_2')
    await waitForAnswer(question, 'completed', 'settled')
    await saveEvidence(email, question, failed.job.id)
    await saveEvidence(email, question, originalResponseJobId)
    if (!retryActionVisible) {
      test.fail(true, '재시도 응답 유실/SSE 단절 시 같은 키 확인 버튼 소실')
      await expect(retryAgain).toBeVisible()
    }
  } finally {
    await page.unroute('**/api/conversations/*/messages/*/retry', retryRoute)
    releaseGate(question, 'attempt_2')
  }
})

type QuestionResponse = { status: number; body: SubmitEnvelope | { error: { code: string } } }

async function submitAndInspect(page: Page, question: string): Promise<QuestionResponse> {
  const result = await submitAndReadStatus(page, question)
  return { status: result.status, body: result.body as QuestionResponse['body'] }
}

async function providerStarts() {
  const response = await fetch(`${process.env.S54_PROBE_URL}/provider-events`)
  expect(response.status).toBe(200)
  return (await response.json()) as { question: string; event: string; attempt: number; outcome: string }[]
}

test('B8: 여러 탭·독립 context에서 rate gate, 실행 1/대기 3/retry 1, FIFO와 대화 내 retry 우선을 확인한다', async ({
  browser,
  page,
}) => {
  const email = await signUp(page, 'b8-multiclient')
  await openMemberChat(page)
  const token = await accessToken(page, email)
  const failedQuestion = uniqueQuestion('script')
  const runningQuestion = uniqueQuestion('script')
  const raceQuestionA = uniqueQuestion('script')
  const raceQuestionB = uniqueQuestion('script')
  const followerQuestion = uniqueQuestion('script')
  const laterQuestion = uniqueQuestion('script')
  const queueFullQuestion = uniqueQuestion('script')
  const otherMemberQuestion = uniqueQuestion('script')
  configureProvider(failedQuestion, ['fail', 'hold'])
  configureProvider(runningQuestion, ['hold'])
  configureProvider(raceQuestionA, ['hold'])
  configureProvider(raceQuestionB, ['hold'])
  configureProvider(followerQuestion, ['hold'])
  configureProvider(laterQuestion, ['hold'])
  configureProvider(queueFullQuestion, ['hold'])
  configureProvider(otherMemberQuestion, ['hold'])

  const gates: [string, `attempt_${number}`][] = [
    [runningQuestion, 'attempt_1'],
    [raceQuestionA, 'attempt_1'],
    [raceQuestionB, 'attempt_1'],
    [followerQuestion, 'attempt_1'],
    [laterQuestion, 'attempt_1'],
    [failedQuestion, 'attempt_2'],
    [otherMemberQuestion, 'attempt_1'],
  ]
  let otherContext: Awaited<ReturnType<Browser['newContext']>> | null = null
  let otherMemberContext: Awaited<ReturnType<Browser['newContext']>> | null = null
  let otherMemberEmail = ''

  try {
    const failed = await submit(page, failedQuestion)
    await waitForState(failedQuestion, 'failed')
    await reopenAdmission(page, email)
    await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
    await newConversation(page)
    const running = await submit(page, runningQuestion)
    await waitForGate(runningQuestion, 'attempt_1')
    expect((await databaseState(runningQuestion)).job_state).toBe('running')
    const runningActions = await expectActions(page, token, runningQuestion, running.job.id)
    expect(runningActions.allowed_actions).toEqual([])

    const sameContextTab = await page.context().newPage()
    await openMemberChat(sameContextTab)
    otherContext = await browser.newContext()
    const otherDevice = await otherContext.newPage()
    await signIn(otherDevice, email)
    await openMemberChat(otherDevice)

    await allowNext(email)
    for (const tab of [sameContextTab, otherDevice]) {
      await tab.reload()
      await openMemberChat(tab)
      await newConversation(tab)
    }
    const ready = Promise.withResolvers<void>()
    const racing = [
      [sameContextTab, raceQuestionA],
      [otherDevice, raceQuestionB],
    ] as const
    const responses = racing.map(async ([tab, question]) => {
      const request = tab.waitForResponse((response) => {
        const call = response.request()
        return call.method() === 'POST' && /\/api\/conversations\/\d+\/messages$/.test(new URL(response.url()).pathname)
      })
      await tab.getByLabel('질문 입력').fill(question)
      await ready.promise
      await tab.getByRole('button', { name: '질문 보내기' }).click()
      const response = await request
      return { question, status: response.status(), body: await response.json() }
    })
    ready.resolve()
    const raceResults = await Promise.all(responses)
    const admitted = raceResults.filter((result) => result.status === 202)
    const refused = raceResults.filter((result) => result.status === 429)
    expect(admitted).toHaveLength(1)
    expect(refused).toHaveLength(1)
    expect(refused[0]?.body.error.code).toBe('attempt_too_soon')
    const accepted = admitted[0]!
    const firstWait = accepted.body as SubmitEnvelope
    expect(firstWait.job.state).toBe('queued')
    expect((await memberState(email)).question_count).toBe(3)

    await reopenAdmission(page, email)
    await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
    const follower = await submit(page, followerQuestion)
    expect(follower.job.state).toBe('paused')
    expect((await expectActions(page, token, followerQuestion, follower.job.id)).paused_by_message_id).toBe(
      failed.question.id,
    )

    await allowNext(email)
    const capacityTab = await page.context().newPage()
    await openMemberChat(capacityTab)
    const staleTab = await page.context().newPage()
    await controlChatEvents(staleTab)
    await openMemberChat(staleTab)
    await setChatEventsBlocked(staleTab, true)
    await newConversation(capacityTab)
    await newConversation(staleTab)
    const later = await submit(capacityTab, laterQuestion)
    expect(later.job.state).toBe('queued')

    await allowNext(email)
    const beforeFull = await memberState(email)
    const overLimit = await submitAndInspect(staleTab, queueFullQuestion)
    expect(overLimit.status).toBe(409)
    expect((overLimit.body as { error: { code: string } }).error.code).toBe('queue_full')
    await expect(staleTab.getByRole('alert')).toContainText('대기 중인 질문이 많습니다')
    const afterFull = await memberState(email)
    expect(afterFull.question_count).toBe(beforeFull.question_count)
    expect(afterFull.reservation_count).toBe(beforeFull.reservation_count)
    expect(afterFull.items.some((item) => item.question === queueFullQuestion)).toBe(false)

    await reopenAdmission(page, email)
    await selectConversation(page, token, failed.job.conversation_id, failedQuestion)
    const retry = await retryFromUi(page, failedQuestion, failed.job.id)
    expect(retry.job.kind).toBe('retry')
    expect(retry.job.state).toBe('queued')
    expect((await expectActions(page, token, failedQuestion, retry.job.id)).allowed_actions).toEqual(['cancel'])
    const fullQueue = await memberState(email)
    expect(fullQueue.limits.queue).toMatchObject({
      running: 1,
      questions_waiting: 3,
      questions_max: 3,
      retries_waiting: 1,
      retries_max: 1,
    })
    expect(fullQueue.question_count).toBe(5)
    expect(fullQueue.reservation_count).toBe(6)

    const savedRaceConversation = firstWait.job.conversation_id
    for (const tab of [page, sameContextTab, otherDevice]) {
      if (tab !== page) {
        await tab.reload()
        await openMemberChat(tab)
      }
      await selectConversation(tab, token, savedRaceConversation, accepted.question)
      await expect(tab.getByText(/새 질문 대기 3\/3/)).toBeVisible()
      await expect(tab.getByText(/재시도 대기 1\/1/)).toBeVisible()
      await expect(tab.getByText(/실행 중 1/)).toBeVisible()
      await expect(jobCard(tab, accepted.question, firstWait.job.id)).toContainText('대기 중')
    }

    otherMemberContext = await browser.newContext()
    const otherMemberPage = await otherMemberContext.newPage()
    otherMemberEmail = await signUp(otherMemberPage, 'b8-other-member')
    await openMemberChat(otherMemberPage)
    const isolated = await submit(otherMemberPage, otherMemberQuestion)
    await waitForGate(otherMemberQuestion, 'attempt_1')
    const otherState = await memberState(otherMemberEmail)
    expect(otherState.limits.queue).toMatchObject({ running: 1, questions_waiting: 0, retries_waiting: 0 })
    expect(otherState.question_count).toBe(1)
    await expect(otherMemberPage.getByText(failedQuestion)).toHaveCount(0)

    releaseGate(runningQuestion, 'attempt_1')
    await waitForGate(accepted.question, 'attempt_1')
    await waitForState(accepted.question, 'running')
    releaseGate(accepted.question, 'attempt_1')
    await waitForGate(laterQuestion, 'attempt_1')
    await waitForState(laterQuestion, 'running')
    releaseGate(laterQuestion, 'attempt_1')
    await waitForGate(failedQuestion, 'attempt_2')
    await waitForState(failedQuestion, 'running')
    expect((await databaseState(followerQuestion)).job_state).toBe('paused')
    releaseGate(failedQuestion, 'attempt_2')
    await waitForGate(followerQuestion, 'attempt_1')
    await waitForState(followerQuestion, 'running')
    releaseGate(followerQuestion, 'attempt_1')

    await waitForAnswer(runningQuestion, 'completed', 'settled')
    await waitForAnswer(accepted.question, 'completed', 'settled')
    await waitForAnswer(laterQuestion, 'completed', 'settled')
    await waitForAnswer(failedQuestion, 'completed', 'settled')
    await waitForAnswer(followerQuestion, 'completed', 'settled')
    await saveEvidence(email, failedQuestion, failed.job.id)
    await saveEvidence(email, runningQuestion, running.job.id)
    await saveEvidence(email, accepted.question, firstWait.job.id)
    await saveEvidence(email, followerQuestion, follower.job.id)
    await saveEvidence(email, laterQuestion, later.job.id)
    await saveEvidence(email, failedQuestion, retry.job.id)
    await saveEvidence(otherMemberEmail, otherMemberQuestion, isolated.job.id)

    const starts = (await providerStarts())
      .filter((event) => event.event === 'started')
      .filter((event) =>
        [failedQuestion, runningQuestion, accepted.question, laterQuestion, followerQuestion].includes(event.question),
      )
      .map(({ question, attempt }) => [question, attempt])
    expect(starts).toEqual([
      [failedQuestion, 1],
      [runningQuestion, 1],
      [accepted.question, 1],
      [laterQuestion, 1],
      [failedQuestion, 2],
      [followerQuestion, 1],
    ])
  } finally {
    for (const [question, stage] of gates) releaseGate(question, stage)
    await otherContext?.close()
    await otherMemberContext?.close()
  }
})
