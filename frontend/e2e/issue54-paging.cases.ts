import { expect, test } from '@playwright/test'
import { signUp } from './issue54.helpers'
import { makeEnvelope, makeLimits, makeMessage } from '../src/features/member-chat/chatQueueBuilders'

// 목록/이력의 큰 데이터는 브라우저 API fixture다. 실제 DB에 수천 개 행을 만들지 않는다.
// 인증·세션 복구·SSE는 기존 격리 mock runner의 실제 API를 사용한다.
test('B7: 일반 홈에서 1,100개 기록을 추가 조회하고 5,500개 메시지의 최신 답변·실패 동작을 연다', async ({ page }) => {
  test.setTimeout(45_000)
  await signUp(page, 'paged-home')
  const conversation = (id: number) => {
    const created = new Date(Date.UTC(2026, 10, 1, 0, id)).toISOString()
    return { id: String(id), created_at: created, updated_at: created }
  }
  const latest = makeEnvelope({
    job: {
      id: '2750',
      conversation_id: '1',
      question_message_id: '5499',
      state: 'failed',
      allowed_actions: ['retry', 'skip'],
    },
    question: { content: '최신 실패 질문' },
    answer: { id: '5500', status: 'failed' },
  })
  const messages = Array.from({ length: 2749 }, (_, i) => [
    makeMessage({ id: String(i * 2 + 1), role: 'user', content: `질문 ${i}` }),
    makeMessage({
      id: String(i * 2 + 2),
      role: 'assistant',
      reply_to_message_id: String(i * 2 + 1),
      content: i === 2748 ? '최신 저장 답변' : `답변 ${i}`,
    }),
  ])
    .flat()
    .concat([latest.question, latest.answer!])
  let listRequests = 0,
    historyRequests = 0
  await page.route('**/api/conversations?*', async (route) => {
    listRequests++
    const before = Number(new URL(route.request().url()).searchParams.get('before_id') ?? 1101)
    const newest = before - 1
    const items = Array.from({ length: Math.min(newest, 20) }, (_, i) => conversation(newest - i))
    await route.fulfill({ json: { items, next_cursor: newest > 20 ? items.at(-1)!.id : null } })
  })
  await page.route('**/api/conversations/*/messages?*', async (route) => {
    historyRequests++
    expect(new URL(route.request().url()).pathname).toBe('/api/conversations/1/messages')
    const start = Number(new URL(route.request().url()).searchParams.get('after_id') ?? 0)
    const items = messages.slice(start, start + 100)
    await route.fulfill({ json: { items, next_cursor: start + 100 < 5500 ? items.at(-1)!.id : null } })
  })
  await page.route('**/api/chat/jobs?*', (route) =>
    route.fulfill({
      json: { items: [latest.job], next_cursor: null, limits: makeLimits(), server_time: new Date().toISOString() },
    }),
  )
  await page.goto('/')
  const history = page.getByRole('navigation', { name: '대화 기록' })
  await expect(history.getByRole('button', { name: /대화 ·/ })).toHaveCount(20)
  expect(historyRequests).toBe(0)
  for (let batch = 2; batch <= 55; batch++) {
    await page.getByRole('button', { name: '기록 더 불러오기', exact: true }).click()
    await expect(history.getByRole('button', { name: /대화 ·/ })).toHaveCount(batch * 20)
  }
  await expect(page.getByRole('button', { name: '기록 더 불러오기', exact: true })).toHaveCount(0)
  expect(listRequests).toBeGreaterThanOrEqual(55) // React StrictMode may replay only the first page.
  await history
    .getByRole('button', { name: /대화 ·/ })
    .last()
    .click()
  await expect(page.getByText('최신 저장 답변', { exact: true })).toBeVisible()
  await expect(page.getByText('최신 실패 질문', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '재시도', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '건너뛰기', exact: true })).toBeVisible()
  expect(historyRequests).toBe(55)
})
