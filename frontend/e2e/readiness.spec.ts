import { expect, test } from '@playwright/test'

test('screen reaches the real API and dedicated test database, then handles an API failure', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'DB 확인' }).click()
  await expect(page.getByText('데이터베이스에 연결되었습니다.')).toBeVisible()

  await page.route('**/api/ready', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'database_unavailable', message: '데이터베이스에 연결할 수 없습니다.' }),
    }),
  )
  await page.getByRole('button', { name: 'DB 확인' }).click()
  await expect(page.getByText('데이터베이스에 연결할 수 없습니다.')).toBeVisible()
})
