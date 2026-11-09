import { appendFileSync, existsSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { join } from 'node:path'
import { expect, type Page } from '@playwright/test'
import type { ChatAdmission, ChatJob, ChatLimits } from '../src/features/member-chat/chatQueueTypes'

export const ORIGIN = process.env.E2E_ORIGIN ?? 'http://127.0.0.1:5174'
const PROBE = process.env.S54_PROBE_URL!
const CONTROL_DIR = process.env.S54_CONTROL_DIR!
const EVIDENCE_FILE = process.env.S54_EVIDENCE_FILE!
export const PASSWORD = 'password123!'

export type DatabaseState = {
  user_id: string
  conversation_id: string
  question_id: string
  question: string
  job_id: string
  job_state: string
  job_kind: string
  accepted_at: string
  expires_at: string
  answer_id: string | null
  answer_status: string | null
  answer_content: string | null
  reservation_id: string | null
  reservation_state: string | null
  refund_reason: string | null
}

export type MemberState = {
  user_id: string
  email: string
  question_count: number
  reservation_count: number
  items: DatabaseState[]
  limits: ChatLimits
}

export type SubmitEnvelope = ChatAdmission & { job: ChatJob }

export type ProviderOutcome = 'fail' | 'interrupted' | 'success' | 'hold'
export type GateStage = 'pre' | 'middle' | 'notify' | `attempt_${number}`
export type ProviderEvent = { question: string; event: 'started' | 'finished'; attempt: number; outcome: string }

const GATES: Record<'pre' | 'middle' | 'notify', string> = {
  pre: 'before_first_delta',
  middle: 'during_delta',
  notify: 'after_commit_before_publish',
}

function token(question: string) {
  return question.split('-').at(-1)!
}

function gateName(stage: GateStage) {
  if (stage === 'pre' || stage === 'middle' || stage === 'notify') return GATES[stage]
  return stage
}

function gatePath(question: string, stage: GateStage) {
  return join(CONTROL_DIR, `${token(question)}.${gateName(stage)}`)
}

export function uniqueQuestion(mode: string) {
  return `issue54-${mode}-${randomBytes(4).toString('hex')}`
}

export function configureProvider(question: string, outcomes: ProviderOutcome[]) {
  writeFileSync(join(CONTROL_DIR, `${token(question)}.script.json`), JSON.stringify(outcomes))
}

export async function signUp(page: Page, name: string) {
  const email = `issue54-${name}-${Date.now()}-${randomBytes(3).toString('hex')}@example.com`
  const response = await page.request.post(`${ORIGIN}/api/auth/register`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(201)
  return email
}

export async function signIn(page: Page, email: string) {
  const response = await page.request.post(`${ORIGIN}/api/auth/login`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(200)
}

export async function openMemberChat(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('button', { name: '로그아웃' })).toBeVisible()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
}

export function thread(page: Page, question: string) {
  return page.locator('[data-question-id]').filter({ hasText: question })
}

export async function submit(page: Page, question: string): Promise<SubmitEnvelope> {
  const responsePromise = page.waitForResponse((response) => {
    const request = response.request()
    return request.method() === 'POST' && /\/api\/conversations\/\d+\/messages$/.test(new URL(response.url()).pathname)
  })
  await page.getByLabel('질문 입력').fill(question)
  await page.getByRole('button', { name: '질문 보내기' }).click()
  const response = await responsePromise
  expect(response.status()).toBe(202)
  return (await response.json()) as SubmitEnvelope
}

export async function waitForGate(question: string, stage: GateStage) {
  await expect.poll(() => existsSync(gatePath(question, stage)), { timeout: 12_000 }).toBe(true)
}

export function releaseGate(question: string, stage: GateStage) {
  writeFileSync(`${gatePath(question, stage)}.release`, 'release')
}

export async function databaseState(question: string): Promise<DatabaseState> {
  const response = await fetch(`${PROBE}/state?question=${encodeURIComponent(question)}`)
  expect(response.status).toBe(200)
  return (await response.json()) as DatabaseState
}

export async function memberState(email: string): Promise<MemberState> {
  const response = await fetch(`${PROBE}/member?email=${encodeURIComponent(email)}`)
  expect(response.status).toBe(200)
  return (await response.json()) as MemberState
}

export async function providerEvents(): Promise<ProviderEvent[]> {
  const response = await fetch(`${PROBE}/provider-events`)
  expect(response.status).toBe(200)
  return (await response.json()) as ProviderEvent[]
}

async function postControl(path: string) {
  const response = await fetch(`${PROBE}${path}`, { method: 'POST' })
  expect(response.status).toBe(200)
  return response.json()
}

export async function ageJob(question: string, seconds: number) {
  return postControl(`/control/age?question=${encodeURIComponent(question)}&seconds=${seconds}`)
}

export async function allowNext(email: string) {
  return postControl(`/control/interval?email=${encodeURIComponent(email)}`)
}

export async function exhaustQuota(email: string) {
  return postControl(`/control/quota?email=${encodeURIComponent(email)}`)
}

export async function saveEvidence(email: string, question: string, jobId: string) {
  appendFileSync(EVIDENCE_FILE, `${JSON.stringify({ email, question, job_id: jobId })}\n`)
}

/** Controls the real event fetch from inside the page without changing the chat app. */
export async function controlChatEvents(page: Page) {
  await page.addInitScript(() => {
    const control = window as unknown as {
      __chatEventsSince: (string | null)[]
      __blockChatEvents: boolean
      __cutChatEvents: () => void
    }
    const open = new Set<AbortController>()
    const realFetch = window.fetch.bind(window)
    control.__chatEventsSince = []
    control.__blockChatEvents = false
    control.__cutChatEvents = () => {
      for (const controller of open) controller.abort()
      open.clear()
    }
    window.fetch = (input, init) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (!url.includes('/api/chat/events')) return realFetch(input, init)
      control.__chatEventsSince.push(new URL(url, location.href).searchParams.get('since'))
      if (control.__blockChatEvents) return Promise.reject(new TypeError('blocked by test'))
      const controller = new AbortController()
      init?.signal?.addEventListener('abort', () => controller.abort(), { once: true })
      open.add(controller)
      return realFetch(input, { ...init, signal: controller.signal })
    }
  })
}

export async function setChatEventsBlocked(page: Page, blocked: boolean) {
  await page.evaluate((value) => {
    const control = window as unknown as { __blockChatEvents: boolean; __cutChatEvents: () => void }
    control.__blockChatEvents = value
    if (value) control.__cutChatEvents()
  }, blocked)
}

/** Drops only the first POST response after the server has received and handled it. */
export async function dropFirstQuestionResponse(page: Page) {
  const posted: { request_key: string }[] = []
  let dropped = false
  await page.route('**/api/conversations/*/messages', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    posted.push(JSON.parse(route.request().postData() ?? '{}'))
    if (dropped) return route.fallback()
    dropped = true
    await route.fetch()
    await route.abort('failed')
  })
  return posted
}
