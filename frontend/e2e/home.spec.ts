import { expect, test } from '@playwright/test'

// API는 모두 고정 fixture로 대체한다. 실제 계정·DB·AI를 사용하지 않는다.
for (const signedIn of [false, true]) {
  test(`${signedIn ? '회원' : '비회원'}도 홈에서 시작하고 명시적으로 선택한다`, async ({ page }) => {
    const chatRequests: string[] = []
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/auth/csrf') {
        return route.fulfill({ status: signedIn ? 200 : 401, json: signedIn ? { csrf_token: 'fixture' } : {} })
      }
      if (path === '/api/auth/refresh') {
        return route.fulfill({ json: { access_token: 'fixture', csrf_token: 'fixture', expires_in: 900 } })
      }
      if (path === '/api/me') {
        return route.fulfill({
          json: { user: { id: '1', email: 'fixture@example.com', created_at: '2026-01-01T00:00:00Z' } },
        })
      }
      if (path === '/api/chat/events') {
        return route.fulfill({ contentType: 'text/event-stream', body: ': ping\n\n' })
      }
      chatRequests.push(`${route.request().method()} ${path}`)
      if (path === '/api/conversations') {
        return route.fulfill({
          json: { items: [{ id: '501', created_at: '2026-01-01T00:00:00Z' }], next_cursor: null },
        })
      }
      return route.fulfill({ json: { items: [], next_cursor: null } })
    })
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /복잡한 교통법/ })).toBeVisible()
    await expect(page.getByLabel('질문 입력', { exact: true })).toHaveCount(0)
    if (signedIn) {
      const history = page.getByRole('navigation', { name: '대화 기록' }).getByRole('button')
      await expect(history).toHaveCount(1)
      await expect(history).not.toHaveAttribute('aria-current', 'page')
      expect(chatRequests.length).toBeGreaterThan(0)
      expect(chatRequests.every((request) => request === 'GET /api/conversations')).toBe(true)
      await history.click()
      await expect(page.getByLabel('질문 입력', { exact: true })).toBeVisible()
    }
    const home = page.getByRole('button', { name: '교통법 AI 홈' })
    for (const key of ['Enter', 'Space']) {
      await home.focus()
      await page.keyboard.press(key)
      await expect(page.getByRole('heading', { name: /복잡한 교통법/ })).toBeVisible()
    }
    await home.click()
    await expect(page.getByRole('button', { name: signedIn ? '로그아웃' : '로그인', exact: true })).toBeVisible()
    expect(chatRequests.some((request) => request.startsWith('POST'))).toBe(false)
    if (signedIn) {
      await page.reload()
      await expect(page.getByRole('heading', { name: /복잡한 교통법/ })).toBeVisible()
      await expect(page.getByLabel('질문 입력', { exact: true })).toHaveCount(0)
    }
  })
}
