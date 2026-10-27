import { expect, type Page, test } from '@playwright/test'

// 실제 API·DB·대기열 실행기(무료 모의 제공자)와 연결한 회원 채팅 검사다. `local test-e2e`의 전체 환경에서 실행한다.
// 모의 제공자는 바로 "모의 답변입니다."를 저장하므로 생성 중 임시 출력·실패·재시도 화면은 단위 검사가 다룬다.

const ORIGIN = 'http://127.0.0.1:5173'
const PASSWORD = 'password123!'
const ANSWER = '모의 답변입니다.'
// 같은 회원의 접수 간격은 10초다(`attempt_too_soon`).
const ATTEMPT_GAP_MS = 10_500

function uniqueEmail(name: string) {
  return `${name}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

/** API로 가입한다. 응답의 refresh 쿠키가 브라우저 컨텍스트에 남아, 화면을 열면 세션을 복구해 바로 회원 화면이 된다. */
async function signUp(page: Page, name: string) {
  const email = uniqueEmail(name)
  const response = await page.request.post(`${ORIGIN}/api/auth/register`, {
    headers: { Origin: ORIGIN },
    data: { email, password: PASSWORD },
  })
  expect(response.status()).toBe(201)
  return email
}

async function openMemberChat(page: Page) {
  await page.goto('/')
  await expect(page.getByRole('button', { name: '로그아웃' })).toBeVisible()
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨')
}

async function ask(page: Page, question: string) {
  await page.getByLabel('질문 입력').fill(question)
  await page.getByRole('button', { name: '질문 보내기' }).click()
}

function thread(page: Page, question: string) {
  return page.locator('[data-question-id]').filter({ hasText: question })
}

test('질문을 접수하면 구독으로 저장된 답변을 받고, 새로고침해도 같은 대화와 답변이 남는다', async ({ page }) => {
  await signUp(page, 'chat-basic')
  await openMemberChat(page)

  await ask(page, '어린이보호구역 제한속도는?')

  await expect(thread(page, '어린이보호구역 제한속도는?')).toContainText(ANSWER)
  await expect(thread(page, '어린이보호구역 제한속도는?')).toContainText('답변 완료 · 저장됨')
  await expect(page.getByLabel('질문 입력')).toHaveValue('')
  await expect(page.getByText(/오늘 남은 질문 399\/400/)).toBeVisible()

  const sidebar = page.getByRole('navigation', { name: '대화 기록' })
  await expect(sidebar.getByRole('button')).toHaveCount(1)
  await expect(sidebar.getByRole('button')).toHaveAttribute('aria-current', 'page')

  await page.reload()
  await expect(thread(page, '어린이보호구역 제한속도는?')).toContainText(ANSWER)
  await expect(sidebar.getByRole('button')).toHaveAttribute('aria-current', 'page')
})

/** 첫 질문 요청은 서버까지 보내 접수시키고, 응답만 화면에 전달하지 않는다. 이후 요청은 그대로 보낸다. */
async function dropFirstQuestionResponse(page: Page) {
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

test('서버가 접수한 뒤 응답을 잃어도 입력이 풀리고, 구독으로 온 작업으로 복구해 다시 보내지 않는다', async ({
  page,
}) => {
  await signUp(page, 'chat-lost')
  await openMemberChat(page)
  const posted = await dropFirstQuestionResponse(page)

  await ask(page, '응답을 잃을 질문')

  await expect(page.getByRole('alert')).toContainText('질문이 접수됐는지 확인하지 못했습니다')
  await expect(page.getByLabel('질문 입력')).toBeEnabled()
  // 접수된 작업과 답변이 구독으로 오면 보관한 키와 입력 내용을 정리한다.
  await expect(thread(page, '응답을 잃을 질문')).toContainText(ANSWER)
  await expect(page.getByLabel('질문 입력')).toHaveValue('')
  await expect(page.getByRole('button', { name: '다시 확인하기' })).toHaveCount(0)
  expect(posted).toHaveLength(1)

  // 새로고침해도 서버에는 질문이 하나뿐이다.
  await page.unroute('**/api/conversations/*/messages')
  await page.reload()
  await expect(thread(page, '응답을 잃을 질문')).toHaveCount(1)
  await expect(page.getByText(/오늘 남은 질문 399\/400/)).toBeVisible()
})

test('구독도 끊긴 채 응답을 잃으면 10초 안에도 같은 키로 다시 확인해 중복 없이 답변을 받는다', async ({ page }) => {
  await controlChatEvents(page)
  await signUp(page, 'chat-lost-offline')
  await openMemberChat(page)
  await page.evaluate(() => {
    const control = window as unknown as { __blockChatEvents: boolean; __cutChatEvents: () => void }
    control.__blockChatEvents = true
    control.__cutChatEvents()
  })
  const posted = await dropFirstQuestionResponse(page)

  await ask(page, '구독 없이 잃을 질문')

  await expect(page.getByRole('alert')).toContainText('질문이 접수됐는지 확인하지 못했습니다')
  // 첫 접수로 10초 간격이 시작됐지만, 같은 질문을 다시 확인하는 일은 막지 않는다.
  await page.getByRole('button', { name: '다시 확인하기' }).click()

  await expect(thread(page, '구독 없이 잃을 질문')).toBeVisible()
  await expect(thread(page, '구독 없이 잃을 질문')).toHaveCount(1)
  expect(posted).toHaveLength(2)
  expect(posted[1]?.request_key).toBe(posted[0]?.request_key)

  await page.evaluate(() => {
    ;(window as unknown as { __blockChatEvents: boolean }).__blockChatEvents = false
  })
  await expect(thread(page, '구독 없이 잃을 질문')).toContainText(ANSWER, { timeout: 20_000 })
})

/**
 * 화면의 구독 연결을 테스트에서 끊고 막을 수 있게 fetch를 감싼다. 앱 코드는 바꾸지 않는다.
 * `__cutChatEvents()`는 열린 구독을 끊고, `__blockChatEvents`가 true인 동안 새 구독은 연결에 실패한다.
 */
async function controlChatEvents(page: Page) {
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

test('구독이 끊긴 사이 끝난 답변은 마지막 server_time을 since로 다시 연결해 snapshot의 저장 결과로 보인다', async ({
  page,
}) => {
  await controlChatEvents(page)
  await signUp(page, 'chat-reconnect')
  await openMemberChat(page)

  await page.evaluate(() => {
    const control = window as unknown as { __blockChatEvents: boolean; __cutChatEvents: () => void }
    control.__blockChatEvents = true
    control.__cutChatEvents()
  })
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 끊김, 다시 연결 중')

  await ask(page, '끊긴 사이 끝날 질문')
  // 접수 응답의 작업은 바로 보인다. 완료 알림은 구독이 끊겨 받지 못한다.
  await expect(thread(page, '끊긴 사이 끝날 질문')).toContainText(/대기 중|답변 생성 중/)
  await expect(thread(page, '끊긴 사이 끝날 질문')).not.toContainText(ANSWER)

  await page.evaluate(() => {
    ;(window as unknown as { __blockChatEvents: boolean }).__blockChatEvents = false
  })
  await expect(page.getByRole('status', { name: '실시간 연결 상태' })).toHaveText('실시간 연결됨', { timeout: 20_000 })
  await expect(thread(page, '끊긴 사이 끝날 질문')).toContainText(ANSWER)

  const sinceValues = await page.evaluate(
    () => (window as unknown as { __chatEventsSince: (string | null)[] }).__chatEventsSince,
  )
  expect(sinceValues[0]).toBeNull()
  expect(sinceValues.at(-1)).toMatch(/^\d{4}-\d{2}-\d{2}T/)
})

test('로그아웃 뒤 다른 회원으로 들어오면 이전 회원의 대화·기록이 보이지 않는다', async ({ page }) => {
  await signUp(page, 'chat-member-a')
  await openMemberChat(page)
  await ask(page, 'A 회원만의 질문')
  await expect(thread(page, 'A 회원만의 질문')).toContainText(ANSWER)

  await page.getByRole('button', { name: '로그아웃' }).click()
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible()
  await expect(page.getByText('A 회원만의 질문')).toHaveCount(0)

  await signUp(page, 'chat-member-b')
  await openMemberChat(page)

  await expect(page.getByText('A 회원만의 질문')).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: '대화 기록' })).toContainText('대화 기록이 아직 없어요.')
  await expect(page.getByText('교통법에 대해 질문해 보세요')).toBeVisible()
})

test('질문이 쌓여 스크롤해도 입력창이 메시지 사이에 끼지 않고 화면 아래에 머문다 (PR #63 제보)', async ({ page }) => {
  test.setTimeout(90_000)
  await page.setViewportSize({ width: 1280, height: 640 })
  await signUp(page, 'chat-layout')
  await openMemberChat(page)

  const questions = ['첫 번째 질문', '두 번째 질문', '세 번째 질문']
  for (const [index, question] of questions.entries()) {
    if (index > 0) await page.waitForTimeout(ATTEMPT_GAP_MS)
    await ask(page, question)
    await expect(thread(page, question)).toContainText(ANSWER)
  }

  const main = page.locator('main.main-content')
  const composer = page.locator('form').filter({ has: page.getByLabel('질문 입력') })
  expect(await main.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true)

  // 맨 아래까지 내리면 마지막 답변이 입력창 위에서 끝난다.
  await main.evaluate((node) => node.scrollTo(0, node.scrollHeight))
  const lastThread = await thread(page, '세 번째 질문').boundingBox()
  const composerAtBottom = await composer.boundingBox()
  expect(lastThread && composerAtBottom).toBeTruthy()
  expect((lastThread?.y ?? 0) + (lastThread?.height ?? 0)).toBeLessThanOrEqual(composerAtBottom?.y ?? 0)

  // 문서 흐름에서도 모든 질문이 입력창보다 앞에 있다.
  const order = await page.evaluate(() => {
    const form = document.querySelector('form')
    return Array.from(document.querySelectorAll('[data-question-id]')).every(
      (node) => form && node.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING,
    )
  })
  expect(order).toBe(true)

  // 맨 위로 올려도 입력창은 화면 아래에 붙어 있다.
  await main.evaluate((node) => node.scrollTo(0, 0))
  const composerAtTop = await composer.boundingBox()
  expect(Math.round((composerAtTop?.y ?? 0) + (composerAtTop?.height ?? 0))).toBeGreaterThan(640 - 40)
  expect(Math.round((composerAtTop?.y ?? 0) + (composerAtTop?.height ?? 0))).toBeLessThanOrEqual(640)
})
