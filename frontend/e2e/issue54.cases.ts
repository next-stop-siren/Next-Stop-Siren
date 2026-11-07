import { appendFileSync, existsSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { join } from 'node:path'
import { expect, type Page, test } from '@playwright/test'

const ORIGIN = process.env.E2E_ORIGIN ?? 'http://127.0.0.1:5173'
const PROBE = process.env.S54_PROBE_URL
const CONTROL_DIR = process.env.S54_CONTROL_DIR
const EVIDENCE_FILE = process.env.S54_EVIDENCE_FILE
const PASSWORD = 'password123!'

function uniqueEmail(name: string) {
  return `${name}-${Date.now()}-${randomBytes(3).toString('hex')}@example.com`
}

function uniqueQuestion(stage: 'pre' | 'middle' | 'notify') {
  return `issue54-${stage}-${randomBytes(4).toString('hex')}`
}

function gatePath(question: string, stage: string) {
  const token = question.split('-')[2]
  return join(CONTROL_DIR!, `${token}.${stage}`)
}

async function waitForGate(question: string, stage: string) {
  await expect.poll(() => existsSync(gatePath(question, stage)), { timeout: 12_000 }).toBe(true)
}

function releaseGate(question: string, stage: string) {
  writeFileSync(`${gatePath(question, stage)}.release`, 'release')
}

async function signUp(page: Page, name: string) {
  const email = uniqueEmail(name)
  const response = await page.request.post(`${ORIGIN}/api/auth/register`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(201)
  return email
}

async function signIn(page: Page, email: string) {
  const response = await page.request.post(`${ORIGIN}/api/auth/login`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(200)
}

async function openMemberChat(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('button', { name: '로그아웃' })).toBeVisible()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
}

function thread(page: Page, question: string) {
  return page.locator('[data-question-id]').filter({ hasText: question })
}

async function submit(page: Page, question: string) {
  const responsePromise = page.waitForResponse((response) => {
    const request = response.request()
    return request.method() === 'POST' && /\/api\/conversations\/\d+\/messages$/.test(new URL(response.url()).pathname)
  })
  await page.getByLabel('질문 입력').fill(question)
  await page.getByRole('button', { name: '질문 보내기' }).click()
  const response = await responsePromise
  expect(response.status()).toBe(202)
  return (await response.json()) as { job: { id: string } }
}

async function databaseState(question: string) {
  const response = await fetch(`${PROBE}/state?question=${encodeURIComponent(question)}`)
  expect(response.status).toBe(200)
  return (await response.json()) as {
    job_id: string
    job_state: string
    answer_status: string | null
    answer_content: string | null
    reservation_state: string
  }
}

async function saveEvidence(email: string, question: string, jobId: string) {
  const record = `${JSON.stringify({ email, question, job_id: jobId })}\n`
  appendFileSync(EVIDENCE_FILE!, record)
}

test('첫 delta 전 탭을 닫아도 작업은 이어지고, 여러 탭·기기에서 같은 회원 작업과 저장 답변을 본다', async ({
  browser,
  page,
}) => {
  test.setTimeout(45_000)
  const email = await signUp(page, 'issue54-pre')
  await openMemberChat(page)
  const question = uniqueQuestion('pre')
  const { job } = await submit(page, question)
  await waitForGate(question, 'before_first_delta')

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
  releaseGate(question, 'before_first_delta')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')

  const restored = await sameContext.newPage()
  await openMemberChat(restored)
  await expect(restored.locator(`[data-job-id="${job.id}"]`)).toContainText('첫 조각 저장 답변')
  await expect(thread(restored, question)).toContainText('답변 완료 · 저장됨')
  const restoredDevice = await deviceContext.newPage()
  await openMemberChat(restoredDevice)
  await expect(restoredDevice.locator(`[data-job-id="${job.id}"]`)).toContainText('첫 조각 저장 답변')
  await saveEvidence(email, question, job.id)
  await deviceContext.close()
})

test('중간 delta를 받은 뒤 새로고침하면 임시 조각은 사라지고 완료된 저장 답변으로 복구한다', async ({ page }) => {
  test.setTimeout(40_000)
  const email = await signUp(page, 'issue54-middle')
  await openMemberChat(page)
  const question = uniqueQuestion('middle')
  const { job } = await submit(page, question)
  await waitForGate(question, 'during_delta')

  await expect(thread(page, question)).toContainText('중간 조각')
  await expect(thread(page, question)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')
  await expect(thread(page, question)).not.toContainText('답변 완료 · 저장됨')
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

  releaseGate(question, 'during_delta')
  await expect.poll(async () => (await databaseState(question)).job_state, { timeout: 12_000 }).toBe('completed')
  await expect(thread(page, question)).toContainText('중간 조각 뒤 저장 답변')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  await saveEvidence(email, question, job.id)
})

test('DB commit 뒤 완료 알림 전 logout·회원 전환해도 A 작업은 저장되고 B에 보이지 않는다', async ({ page }) => {
  test.setTimeout(45_000)
  const emailA = await signUp(page, 'issue54-notify-a')
  await openMemberChat(page)
  const question = uniqueQuestion('notify')
  const { job } = await submit(page, question)
  await waitForGate(question, 'after_commit_before_publish')

  expect(await databaseState(question)).toMatchObject({
    job_id: job.id,
    job_state: 'completed',
    answer_status: 'completed',
    answer_content: '완료 알림 직전 저장 답변',
    reservation_state: 'settled',
  })
  await expect(thread(page, question)).toContainText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')
  await expect(thread(page, question)).not.toContainText('답변 완료 · 저장됨')

  await page.getByRole('button', { name: '로그아웃' }).click()
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible()
  await expect(page.getByText(question)).toHaveCount(0)
  const emailB = await signUp(page, 'issue54-notify-b')
  expect(emailB).not.toBe(emailA)
  await openMemberChat(page)
  await expect(page.getByText(question)).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: '대화 기록' })).toContainText('대화 기록이 아직 없어요.')

  releaseGate(question, 'after_commit_before_publish')
  await signIn(page, emailA)
  await openMemberChat(page)
  await expect(page.locator(`[data-job-id="${job.id}"]`)).toContainText('완료 알림 직전 저장 답변')
  await expect(thread(page, question)).toContainText('답변 완료 · 저장됨')
  await saveEvidence(emailA, question, job.id)
})
