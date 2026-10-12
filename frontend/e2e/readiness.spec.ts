import { expect, test } from '@playwright/test'

// 비회원 소개 화면으로 교체되면서 'DB 확인' 버튼은 더 이상 존재하지 않는다.
// 이 검사의 목적은 UI 버튼 자체가 아니라 실제 API와 전용 테스트 데이터베이스의
// 연결 성공/실패 처리이므로, 페이지 컨텍스트에서 /api/ready를 직접 호출해 확인한다.
// page.evaluate 안에서 fetch를 호출해야 이후의 page.route 모킹이 적용된다.
test('real API and dedicated test database connectivity, including failure handling', async ({ page }) => {
  await page.goto('/')

  const success = await page.evaluate(async () => {
    const response = await fetch('/api/ready', { headers: { Accept: 'application/json' } })
    return { ok: response.ok, status: response.status, body: await response.json() }
  })
  expect(success.ok).toBe(true)
  expect(success.body.status).toBe('ready')

  await page.route('**/api/ready', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'database_unavailable', message: '데이터베이스에 연결할 수 없습니다.' }),
    }),
  )

  const failure = await page.evaluate(async () => {
    const response = await fetch('/api/ready', { headers: { Accept: 'application/json' } })
    return { ok: response.ok, status: response.status, body: await response.json() }
  })
  expect(failure.ok).toBe(false)
  expect(failure.status).toBe(503)
  expect(failure.body.message).toBe('데이터베이스에 연결할 수 없습니다.')
})
