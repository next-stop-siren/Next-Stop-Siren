import { expect, type Page } from '@playwright/test'

/** 홈에서 사용자가 최신 기록 또는 새 대화를 명시적으로 선택하는 테스트 준비 동작. */
export async function chooseMemberChat(page: Page) {
  const history = page.getByRole('navigation', { name: '대화 기록' })
  await expect
    .poll(
      async () =>
        (await history.getByRole('button').count()) > 0 ||
        (await history.getByText('대화 기록이 아직 없어요.').count()) > 0,
    )
    .toBe(true)
  if (await history.getByRole('button').count()) await history.getByRole('button').first().click()
  else await page.getByRole('button', { name: '＋ 새 대화' }).click()
}
