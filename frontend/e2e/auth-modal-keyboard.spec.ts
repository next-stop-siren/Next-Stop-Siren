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

  test('Tab으로 질문 입력창을 지나가기만 하면 모달이 열리지 않고 입력 영역에 초점이 보인다', async ({ page }) => {
    await page.goto('/')

    const promptInput = page.getByRole('textbox', { name: '질문하려면 로그인 또는 회원가입이 필요합니다' })
    await expect(promptInput).toBeVisible()

    for (let i = 0; i < 20; i += 1) {
      await page.keyboard.press('Tab')
      if (await promptInput.evaluate((node) => node === document.activeElement)) break
    }
    await expect(promptInput).toBeFocused()
    await expect(page.getByRole('dialog')).toHaveCount(0)

    // 입력창 외곽선을 지운 대신 감싼 입력 영역이 키보드 초점을 보여준다.
    const outlineStyle = await promptInput.evaluate(
      (node) => getComputedStyle(node.parentElement as HTMLElement).outlineStyle,
    )
    expect(outlineStyle).toBe('solid')

    await page.keyboard.press('Tab')
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  for (const key of ['Enter', 'Space']) {
    test(`질문 입력창에서 ${key}로 열고, Escape 뒤 초점 복귀와 재진입이 동작한다`, async ({ page }) => {
      await page.goto('/')

      const promptInput = page.getByRole('textbox', { name: '질문하려면 로그인 또는 회원가입이 필요합니다' })
      await promptInput.focus()
      await expect(page.getByRole('dialog')).toHaveCount(0)

      const dialog = page.getByRole('dialog')
      await page.keyboard.press(key)
      await expect(dialog).toBeVisible()
      // 여는 키 입력이 모달 안 첫 버튼(닫기)을 함께 누르지 않는다.
      await page.waitForTimeout(100)
      await expect(dialog).toBeVisible()
      await expect(dialog.locator(':focus')).toBeVisible()

      await page.keyboard.press('Escape')
      await expect(dialog).toHaveCount(0)
      await expect(promptInput).toBeFocused()
      await page.waitForTimeout(100)
      await expect(dialog).toHaveCount(0)

      await page.keyboard.press(key)
      await expect(dialog).toBeVisible()
    })
  }

  test('느린 로그인 실패는 닫았다 다시 연 모달의 상태와 초점을 바꾸지 않는다', async ({ page }) => {
    let releaseLogin: () => void = () => {}
    const loginReleased = new Promise<void>((resolve) => {
      releaseLogin = resolve
    })
    await page.route('**/api/auth/login', async (route) => {
      await loginReleased
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'unauthenticated', message: '늦은 실패', fields: {}, trace_id: 'e2e' },
        }),
      })
    })

    await page.goto('/')
    const loginButton = page.getByRole('button', { name: '로그인', exact: true })
    await loginButton.click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('이메일').fill('slow@test.com')
    await dialog.getByLabel('비밀번호').fill('password123456')
    await dialog.getByLabel('비밀번호').press('Enter')
    await expect(dialog.getByRole('button', { name: '처리 중...' })).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(loginButton).toBeFocused()

    await loginButton.press('Enter')
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '회원가입' }).click()
    const emailInput = dialog.getByLabel('이메일')
    await emailInput.focus()

    releaseLogin()
    await page.waitForTimeout(300)

    await expect(dialog.getByRole('heading', { name: '회원가입' })).toBeVisible()
    await expect(dialog.getByRole('alert')).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: '회원가입', exact: true }).last()).toBeEnabled()
    await expect(emailInput).toBeFocused()
  })

  test('느린 로그인 실패는 같은 모달에서 바꾼 회원가입 모드에 오류를 남기지 않는다', async ({ page }) => {
    let releaseLogin: () => void = () => {}
    const loginReleased = new Promise<void>((resolve) => {
      releaseLogin = resolve
    })
    await page.route('**/api/auth/login', async (route) => {
      await loginReleased
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          error: { code: 'unauthenticated', message: '늦은 실패', fields: {}, trace_id: 'e2e' },
        }),
      })
    })

    await page.goto('/')
    await page.getByRole('button', { name: '로그인', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('이메일').fill('slow@test.com')
    await dialog.getByLabel('비밀번호').fill('password123456')
    await dialog.getByLabel('비밀번호').press('Enter')
    await expect(dialog.getByRole('button', { name: '처리 중...' })).toBeVisible()

    await dialog.getByRole('button', { name: '회원가입' }).first().press('Enter')
    await expect(dialog.getByRole('heading', { name: '회원가입' })).toBeVisible()
    const emailInput = dialog.getByLabel('이메일')
    await emailInput.focus()

    releaseLogin()
    await page.waitForTimeout(300)

    await expect(dialog.getByRole('alert')).toHaveCount(0)
    await expect(emailInput).toBeFocused()
  })

  test('회원가입 모드는 이름 입력 없이 이메일·비밀번호만 요구한다', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: '로그인', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('button', { name: '회원가입' }).click()

    await expect(dialog.getByLabel('이름')).toHaveCount(0)
    await expect(dialog.locator('input[required]')).toHaveCount(2)
  })
})
