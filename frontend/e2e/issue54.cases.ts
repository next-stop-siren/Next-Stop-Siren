import { expect, test } from '@playwright/test'
import {
  PASSWORD,
  controlChatEvents,
  databaseState,
  memberState,
  openMemberChat,
  providerEvents,
  releaseGate,
  saveEvidence,
  setChatEventsBlocked,
  signIn,
  signUp,
  submit,
  thread,
  uniqueQuestion,
  waitForGate,
} from './issue54.helpers'

test('B1 - 202 접수 뒤 저장된 답변은 새로고침 뒤 같은 작업으로 복구된다', async ({ page }) => {
  const email = await signUp(page, 'b1')
  await openMemberChat(page)
  const question = uniqueQuestion('normal')
  const accepted = await submit(page, question)

  expect(accepted.job.state).toMatch(/queued|running|completed/)
  await expect(thread(page, question)).toContainText('모의 답변입니다.')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  const saved = await databaseState(question)
  expect(saved).toMatchObject({
    job_id: accepted.job.id,
    job_state: 'completed',
    answer_status: 'completed',
    answer_content: '모의 답변입니다.',
    reservation_state: 'settled',
  })
  const before = await memberState(email)
  expect([before.question_count, before.reservation_count]).toEqual([1, 1])
  await saveEvidence(email, question, accepted.job.id)

  await page.reload()
  await expect(thread(page, question)).toContainText('모의 답변입니다.')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  expect(await memberState(email)).toMatchObject({ question_count: 1, reservation_count: 1 })
})

test('B3 - 첫 delta 전 모든 탭을 닫아도 서버 작업은 완료되고 두 세션에서 같은 저장 답변을 복구한다', async ({
  browser,
  page,
}) => {
  test.setTimeout(45_000)
  const email = await signUp(page, 'b3')
  await openMemberChat(page)
  const question = uniqueQuestion('pre')
  const { job } = await submit(page, question)
  await waitForGate(question, 'pre')

  await expect(thread(page, question)).toContainText('답변 생성 중입니다.')
  expect(await databaseState(question)).toMatchObject({
    job_id: job.id,
    job_state: 'running',
    answer_status: 'pending',
    answer_content: null,
    reservation_state: 'reserved',
  })

  const sameContext = page.context()
  const otherTab = await sameContext.newPage()
  await openMemberChat(otherTab)
  const deviceContext = await browser.newContext()
  const device = await deviceContext.newPage()
  await signIn(device, email)
  await openMemberChat(device)
  for (const tab of [otherTab, device]) {
    await expect(tab.locator(`[data-job-id="${job.id}"]`)).toContainText('답변 생성 중입니다.')
    await expect(tab.getByText(/실행 중 1/)).toBeVisible()
    await expect(tab.getByText(/오늘 남은 질문 399\/400/)).toBeVisible()
  }

  await page.close()
  await otherTab.close()
  await device.close()
  await expect.poll(async () => (await databaseState(question)).job_state).toBe('running')
  releaseGate(question, 'pre')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')

  const restored = await sameContext.newPage()
  await openMemberChat(restored)
  await expect(restored.locator(`[data-job-id="${job.id}"]`)).toContainText('첫 조각 저장 답변')
  await expect(thread(restored, question)).toContainText('답변 완료 · 저장됨')
  const restoredDevice = await deviceContext.newPage()
  await openMemberChat(restoredDevice)
  await expect(restoredDevice.locator(`[data-job-id="${job.id}"]`)).toContainText('첫 조각 저장 답변')
  const state = await memberState(email)
  expect([state.question_count, state.reservation_count]).toEqual([1, 1])
  await saveEvidence(email, question, job.id)
  await deviceContext.close()
})

test('B4 - 중간 delta 뒤 SSE 단절 중 완료된 결과는 재연결 snapshot에서 복구된다', async ({ page }) => {
  test.setTimeout(40_000)
  await controlChatEvents(page)
  const email = await signUp(page, 'b4-sse')
  await openMemberChat(page)
  const question = uniqueQuestion('middle')
  const { job } = await submit(page, question)
  await waitForGate(question, 'middle')
  await expect(thread(page, question)).toContainText('중간 조각')
  await expect(thread(page, question)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')

  await setChatEventsBlocked(page, true)
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 끊김, 다시 연결 중')
  releaseGate(question, 'middle')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')
  await expect(thread(page, question)).not.toContainText('답변 완료 · 저장됨')

  await setChatEventsBlocked(page, false)
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨', { timeout: 20_000 })
  await expect(thread(page, question)).toContainText('중간 조각 뒤 저장 답변')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  const state = await databaseState(question)
  expect(state).toMatchObject({ job_id: job.id, answer_status: 'completed', reservation_state: 'settled' })
  expect(await memberState(email)).toMatchObject({ question_count: 1, reservation_count: 1 })
  await saveEvidence(email, question, job.id)
})

test('B4 - 새로고침은 임시 delta를 지우고 완료 뒤 저장된 전체 답변을 불러온다', async ({ page }) => {
  test.setTimeout(40_000)
  const email = await signUp(page, 'b4-reload')
  await openMemberChat(page)
  const question = uniqueQuestion('middle')
  const { job } = await submit(page, question)
  await waitForGate(question, 'middle')

  await expect(thread(page, question)).toContainText('중간 조각')
  await expect(thread(page, question)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')
  expect(await databaseState(question)).toMatchObject({
    job_id: job.id,
    job_state: 'running',
    answer_status: 'pending',
    answer_content: null,
    reservation_state: 'reserved',
  })

  await page.reload()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
  await expect(thread(page, question)).toContainText('답변 생성 중입니다.')
  await expect(thread(page, question)).not.toContainText('중간 조각')
  await expect(thread(page, question)).not.toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')

  releaseGate(question, 'middle')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')
  await expect(thread(page, question)).toContainText('중간 조각 뒤 저장 답변')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  await expect(memberState(email)).resolves.toMatchObject({ question_count: 1, reservation_count: 1 })
  await saveEvidence(email, question, job.id)
})

test('B5 - commit 뒤 알림 전에 탭을 닫아도 재접속이 같은 저장 결과를 복구한다', async ({ browser, page }) => {
  test.setTimeout(40_000)
  const email = await signUp(page, 'b5')
  await openMemberChat(page)
  const question = uniqueQuestion('notify')
  const { job } = await submit(page, question)
  await waitForGate(question, 'notify')

  expect(await databaseState(question)).toMatchObject({
    job_id: job.id,
    job_state: 'completed',
    answer_status: 'completed',
    answer_content: '완료 알림 직전 저장 답변',
    reservation_state: 'settled',
  })
  await expect(thread(page, question)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')
  await expect(thread(page, question)).not.toContainText('답변 완료 · 저장됨')

  await page.close()
  releaseGate(question, 'notify')
  const reconnectContext = await browser.newContext()
  const reconnect = await reconnectContext.newPage()
  await signIn(reconnect, email)
  await openMemberChat(reconnect)
  await expect(reconnect.locator(`[data-job-id="${job.id}"]`)).toContainText('완료 알림 직전 저장 답변')
  await expect(thread(reconnect, question)).toContainText('답변 완료 · 저장됨')
  const state = await memberState(email)
  expect([state.question_count, state.reservation_count]).toEqual([1, 1])
  const calls = (await providerEvents()).filter((event) => event.question === question && event.event === 'started')
  expect(calls).toHaveLength(1)
  await saveEvidence(email, question, job.id)
  await reconnectContext.close()
})

test('B6 - A의 임시 출력과 늦은 이력 응답은 같은 탭에서 B로 전환한 화면에 섞이지 않고 A 재로그인 때 복구된다', async ({
  browser,
  page,
}) => {
  test.setTimeout(45_000)
  const emailA = await signUp(page, 'b6-a')
  await openMemberChat(page)
  const question = uniqueQuestion('middle')
  const { job } = await submit(page, question)
  await waitForGate(question, 'middle')
  await expect(thread(page, question)).toContainText('중간 조각')

  const contextB = await browser.newContext()
  const pageB = await contextB.newPage()
  const emailB = await signUp(pageB, 'b6-b')
  await openMemberChat(pageB)
  const questionB = uniqueQuestion('middle')
  const jobB = (await submit(pageB, questionB)).job
  await waitForGate(questionB, 'middle')
  await expect(thread(pageB, questionB)).toContainText('중간 조각')

  await page.getByRole('button', { name: '＋ 새 대화' }).click()
  await page.evaluate(() => {
    const state = window as unknown as {
      __issue54HoldHistory: boolean
      __issue54HistoryHeld: boolean
      __issue54HistoryReturned: boolean
      __issue54ReleaseHistory: () => void
    }
    const realFetch = window.fetch.bind(window)
    state.__issue54HoldHistory = true
    state.__issue54HistoryHeld = false
    state.__issue54HistoryReturned = false
    let release!: () => void
    const wait = new Promise<void>((resolve) => {
      release = resolve
    })
    state.__issue54ReleaseHistory = release
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (state.__issue54HoldHistory && /\/api\/conversations\/\d+\/messages\?limit=100/.test(url)) {
        state.__issue54HoldHistory = false
        const response = await realFetch(input, { ...init, signal: undefined })
        const body = await response.clone().text()
        state.__issue54HistoryHeld = true
        await wait
        state.__issue54HistoryReturned = true
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        })
      }
      return realFetch(input, init)
    }
  })
  const conversations = page.getByRole('navigation', { name: '대화 기록' }).getByRole('button')
  await conversations.first().click()
  await expect
    .poll(() => page.evaluate(() => (window as unknown as { __issue54HistoryHeld: boolean }).__issue54HistoryHeld))
    .toBe(true)

  await page.getByRole('button', { name: '로그아웃' }).click()
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible()
  await page.evaluate((text) => {
    const state = window as unknown as { __issue54LeakQuestion: string; __issue54Leaked: boolean }
    state.__issue54LeakQuestion = text
    state.__issue54Leaked = false
    new MutationObserver(() => {
      const main = document.querySelector('main')?.innerText ?? ''
      if (main.includes(state.__issue54LeakQuestion)) state.__issue54Leaked = true
    }).observe(document.body, { subtree: true, childList: true, characterData: true })
  }, question)
  await page.getByRole('button', { name: '로그인', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('이메일').fill(emailB)
  await dialog.getByLabel('비밀번호').fill(PASSWORD)
  await dialog.locator('form').getByRole('button', { name: '로그인', exact: true }).click()
  await expect(page.getByRole('button', { name: '로그아웃' })).toBeVisible()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
  await expect(thread(page, question)).toHaveCount(0)
  await expect(page.locator(`[data-job-id="${jobB.id}"]`)).toContainText('답변 생성 중입니다.')
  await expect(page.getByText(/오늘 남은 질문 399\/400/)).toBeVisible()
  await expect(page.getByLabel('질문 입력')).toHaveValue('')
  expect(await memberState(emailB)).toMatchObject({ question_count: 1, reservation_count: 1 })
  await page.getByLabel('질문 입력').fill('B의 미전송 초안')

  await page.evaluate(() => {
    const state = window as unknown as { __issue54ReleaseHistory: () => void }
    state.__issue54ReleaseHistory()
  })
  await expect
    .poll(() =>
      page.evaluate(() => (window as unknown as { __issue54HistoryReturned: boolean }).__issue54HistoryReturned),
    )
    .toBe(true)
  await page.evaluate(
    () => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))),
  )
  expect(await page.evaluate(() => (window as unknown as { __issue54Leaked: boolean }).__issue54Leaked)).toBe(false)
  await expect(thread(page, question)).toHaveCount(0)
  await expect(page.locator(`[data-job-id="${jobB.id}"]`)).toContainText('답변 생성 중입니다.')
  await expect(page.getByLabel('질문 입력')).toHaveValue('B의 미전송 초안')

  releaseGate(question, 'middle')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')
  await expect(thread(page, question)).toHaveCount(0)
  expect(await databaseState(questionB)).toMatchObject({
    job_id: jobB.id,
    job_state: 'running',
    reservation_state: 'reserved',
  })
  expect(await memberState(emailB)).toMatchObject({ question_count: 1, reservation_count: 1 })
  await expect(page.locator(`[data-job-id="${jobB.id}"]`)).toContainText('답변 생성 중입니다.')
  await expect(thread(pageB, questionB)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')
  await expect(page.getByText(/오늘 남은 질문 399\/400/)).toBeVisible()
  await expect(page.getByLabel('질문 입력')).toHaveValue('B의 미전송 초안')

  releaseGate(questionB, 'middle')
  await expect.poll(async () => (await databaseState(questionB)).job_state, { timeout: 12_000 }).toBe('completed')
  await expect(page.locator(`[data-job-id="${jobB.id}"]`)).toContainText('중간 조각 뒤 저장 답변')
  await expect(pageB.locator(`[data-job-id="${jobB.id}"]`)).toContainText('중간 조각 뒤 저장 답변')

  await page.getByRole('button', { name: '로그아웃' }).click()
  await page.getByRole('button', { name: '로그인', exact: true }).click()
  const aDialog = page.getByRole('dialog')
  await aDialog.getByLabel('이메일').fill(emailA)
  await aDialog.getByLabel('비밀번호').fill(PASSWORD)
  await aDialog.locator('form').getByRole('button', { name: '로그인', exact: true }).click()
  await expect(page.getByRole('button', { name: '로그아웃' })).toBeVisible()
  await expect(page.locator(`[data-job-id="${job.id}"]`)).toContainText('중간 조각 뒤 저장 답변')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  expect(await memberState(emailA)).toMatchObject({ question_count: 1, reservation_count: 1 })
  await saveEvidence(emailA, question, job.id)
  await saveEvidence(emailB, questionB, jobB.id)
  await contextB.close()
})
