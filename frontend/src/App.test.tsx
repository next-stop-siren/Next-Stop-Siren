// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('shows loading and then successful database readiness', async () => {
  let complete!: (response: Response) => void
  const pending = new Promise<Response>((resolve) => { complete = resolve })
  vi.stubGlobal('fetch', vi.fn(() => pending))
  const user = userEvent.setup()
  render(<App />)

  await user.click(screen.getByRole('button', { name: 'DB 확인' }))
  expect(screen.getByText('확인 중…')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'DB 확인' }) as HTMLButtonElement).disabled).toBe(true)
  complete(new Response(JSON.stringify({ status: 'ready' }), { status: 200 }))
  expect(await screen.findByText('데이터베이스에 연결되었습니다.')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'DB 확인' }) as HTMLButtonElement).disabled).toBe(false)
})

test('shows a sanitized failure and succeeds when retried', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'database_unavailable', message: '데이터베이스에 연결할 수 없습니다.' }), { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: 'ready' }), { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  const user = userEvent.setup()
  render(<App />)

  await user.click(screen.getByRole('button', { name: 'DB 확인' }))
  expect(await screen.findByText('데이터베이스에 연결할 수 없습니다.')).toBeTruthy()
  await user.click(screen.getByRole('button', { name: 'DB 확인' }))
  expect(await screen.findByText('데이터베이스에 연결되었습니다.')).toBeTruthy()
  expect(fetch).toHaveBeenCalledTimes(2)
})
