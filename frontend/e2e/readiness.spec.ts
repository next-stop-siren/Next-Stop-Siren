import { expect, test } from '@playwright/test'

// Import the actual Vite-served helper: this covers API error handling, not an error UI.
test('checkApi reads the real test database readiness and rejects a mocked 503', async ({ page }) => {
  await page.goto('/')

  const success = await page.evaluate(async () => {
    const modulePath = '/src/lib/api.ts'
    const { checkApi } = await import(modulePath)
    return checkApi('ready')
  })
  expect(success).toBe('ready')

  await page.route('**/api/ready', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'database_unavailable', message: '데이터베이스에 연결할 수 없습니다.' }),
    }),
  )

  const failure = await page.evaluate(async () => {
    const modulePath = '/src/lib/api.ts'
    const { checkApi } = await import(modulePath)
    try {
      await checkApi('ready')
      return null
    } catch (error) {
      if (error instanceof Error) return error.message
      throw error
    }
  })
  expect(failure).toBe('데이터베이스에 연결할 수 없습니다.')
})
