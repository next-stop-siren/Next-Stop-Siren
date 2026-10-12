import { expect, test } from '@playwright/test'

test.describe('로그인 모달 키보드 동작', () => {
  test('열릴 때 모달 내부로 초점이 이동한다', async ({ page }) => {
    await page.goto('/')

    await page.getByRole('button', { name: '로그인', exact: true }).click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.locator(':focus')).toBeVisible()
  })

  test('Tab과 Shift+Tab이 모달 밖으로 빠져나가지 않는다', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: '로그인', exact: true }).click()

    const dialog = page.getByRole('dialog')
    const focusableCount = await dialog.locator('button, input').count()

    // Tab을 모달 안 요소 수보다 한 번 더 눌러도 여전히 모달 안에 머물러야 한다.
    for (let i = 0; i < focusableCount + 1; i += 1) {
      await page.keyboard.press('Tab')
      const activeInDialog = await dialog.evaluate((node) => node.contains(document.activeElement))
      expect(activeInDialog).toBe(true)
    }

    // Shift+Tab으로 반대 방향도 동일하게 모달 안에 머물러야 한다.
    for (let i = 0; i < focusableCount + 1; i += 1) {
      await page.keyboard.press('Shift+Tab')
      const activeInDialog = await dialog.evaluate((node) => node.contains(document.activeElement))
      expect(activeInDialog).toBe(true)
    }
  })

  test('Escape로 닫히고, 모달을 연 로그인 버튼으로 초점이 복원된다', async ({ page }) => {
    await page.goto('/')

    const loginButton = page.getByRole('button', { name: '로그인', exact: true })
    await loginButton.click()
    await expect(page.getByRole('dialog')).toBeVisible()

    await page.keyboard.press('Escape')

    await expect(page.getByRole('dialog')).not.toBeVisible()
    await expect(loginButton).toBeFocused()
  })

  test('질문 입력창에서 연 모달을 닫으면 초점이 입력창으로 복원되지만 모달이 다시 열리지 않는다', async ({ page }) => {
    await page.goto('/')

    const promptInput = page.getByRole('textbox', { name: '질문하려면 로그인 또는 회원가입이 필요합니다' })
    await promptInput.click()

    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()

    // 포커스가 입력창으로 돌아오지만, 이 복원 자체가 onFocus를 다시 트리거해
    // 모달을 즉시 재오픈해서는 안 된다.
    await expect(promptInput).toBeFocused()
    await page.waitForTimeout(100)
    await expect(dialog).not.toBeVisible()
  })
})
