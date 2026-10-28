import { expect, test } from '@playwright/test'

// 실제 Google·API 없이 서버가 돌려준 실패 Location을 직접 열어 화면 연결만 확인한다.
// 대체 제공자 서버 검사와 실제 Google 실행은 별도 증거로 기록한다.
test.describe('Google 로그인 실패 복귀 화면', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/auth/csrf', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'unauthenticated', message: '인증 필요', fields: {}, trace_id: 't' } }),
      }),
    )
  })

  test('실패 안내를 보여주고 새로고침 때 반복하지 않는다', async ({ page }) => {
    await page.goto('/?google_login_error=cancelled')

    const dialog = page.getByRole('dialog', { name: 'Google 로그인을 취소했어요' })
    await expect(dialog).toBeVisible()
    await expect(page.getByRole('link', { name: 'Google로 다시 시도하기' })).toBeFocused()
    await expect(page).toHaveURL(/\/$/)

    await page.reload()
    await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible()
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('키보드로 이메일 로그인으로 넘어가고 초점이 모달 안에 머문다', async ({ page }) => {
    await page.goto('/?google_login_error=temporarily_unavailable')

    const dialog = page.getByRole('dialog', { name: 'Google 로그인에 실패했어요' })
    await expect(dialog).toBeVisible()

    for (let i = 0; i < 4; i += 1) {
      await page.keyboard.press('Tab')
      expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true)
    }

    await page.getByRole('button', { name: '이메일로 로그인' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('dialog', { name: '다시 만나서 반가워요' })).toBeVisible()
    await expect(page.getByLabel('이메일')).toBeFocused()
  })

  test('다시 시도는 Google 시작 주소로 전체 페이지 이동한다', async ({ page }) => {
    await page.route('**/api/auth/google/start', (route) =>
      route.fulfill({ status: 200, contentType: 'text/plain', body: 'google start' }),
    )
    await page.goto('/?google_login_error=invalid_request')

    await page.getByRole('link', { name: 'Google로 다시 시도하기' }).click()
    await expect(page).toHaveURL(/\/api\/auth\/google\/start$/)
  })

  test('알 수 없는 값의 원문은 화면에 나오지 않는다', async ({ page }) => {
    await page.goto('/?google_login_error=%3Cimg%20src%3Dx%3Eleak')

    await expect(page.getByRole('dialog', { name: 'Google 로그인에 실패했어요' })).toBeVisible()
    await expect(page.locator('body')).not.toContainText('leak')
    await expect(page).toHaveURL(/\/$/)
  })
})
