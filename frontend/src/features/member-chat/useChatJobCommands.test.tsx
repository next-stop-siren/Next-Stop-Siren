// @vitest-environment jsdom
import { afterEach, expect, test, vi, type Mock } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useChatJobCommands, type ChatJobCommandApi } from './useChatJobCommands'

afterEach(() => {
  cleanup()
})

type Api = {
  retry: Mock<ChatJobCommandApi['retry']>
  skip: Mock<ChatJobCommandApi['skip']>
  cancel: Mock<ChatJobCommandApi['cancel']>
}

function makeApi(overrides: Partial<Api> = {}): Api {
  return {
    retry: vi.fn<ChatJobCommandApi['retry']>(async () => undefined),
    skip: vi.fn<ChatJobCommandApi['skip']>(async () => undefined),
    cancel: vi.fn<ChatJobCommandApi['cancel']>(async () => undefined),
    ...overrides,
  }
}

test('재시도 요청이 실패하면 다시 눌렀을 때 같은 retry_key로 보낸다', async () => {
  const api = makeApi({
    retry: vi.fn<ChatJobCommandApi['retry']>().mockRejectedValueOnce(new Error('network')).mockResolvedValue(undefined),
  })
  const { result } = renderHook(() => useChatJobCommands('user-1', api))

  await act(async () => {
    await result.current.retry('7001').catch(() => undefined)
  })
  await act(async () => {
    await result.current.retry('7001')
  })

  expect(api.retry).toHaveBeenCalledTimes(2)
  const [first, second] = api.retry.mock.calls
  expect(first?.[1]).toBeTruthy()
  expect(second?.[1]).toBe(first?.[1])
})

test('접수가 확인되면 키를 버리고, 다음 재시도는 새 키를 쓴다', async () => {
  const api = makeApi()
  const { result } = renderHook(() => useChatJobCommands('user-1', api))

  await act(async () => {
    await result.current.retry('7001')
  })
  await act(async () => {
    await result.current.retry('7001')
  })

  const [first, second] = api.retry.mock.calls
  expect(second?.[1]).not.toBe(first?.[1])
})

test('다른 작업의 재시도는 다른 키를 쓴다', async () => {
  const api = makeApi({ retry: vi.fn<ChatJobCommandApi['retry']>().mockRejectedValue(new Error('network')) })
  const { result } = renderHook(() => useChatJobCommands('user-1', api))

  await act(async () => {
    await result.current.retry('7001').catch(() => undefined)
    await result.current.retry('7002').catch(() => undefined)
  })

  const [first, second] = api.retry.mock.calls
  expect(second?.[1]).not.toBe(first?.[1])
})

test('같은 재시도가 진행 중일 때 다시 호출하면 새로 보내지 않는다', async () => {
  let release: () => void = () => undefined
  const api = makeApi({
    retry: vi.fn<ChatJobCommandApi['retry']>(
      () =>
        new Promise<undefined>((resolve) => {
          release = () => resolve(undefined)
        }),
    ),
  })
  const { result } = renderHook(() => useChatJobCommands('user-1', api))

  let both: Promise<unknown> = Promise.resolve()
  act(() => {
    both = Promise.all([result.current.retry('7001'), result.current.retry('7001')])
  })

  expect(api.retry).toHaveBeenCalledTimes(1)

  await act(async () => {
    release()
    await both
  })
})

test('건너뛰기와 취소도 진행 중에는 한 번만 보낸다', async () => {
  const api = makeApi()
  const { result } = renderHook(() => useChatJobCommands('user-1', api))

  await act(async () => {
    await Promise.all([result.current.skip('7001'), result.current.skip('7001')])
    await Promise.all([result.current.cancel('7002'), result.current.cancel('7002')])
  })

  expect(api.skip).toHaveBeenCalledTimes(1)
  expect(api.cancel).toHaveBeenCalledTimes(1)
})

test('사용자가 바뀌면 이전 사용자의 retry_key를 쓰지 않는다', async () => {
  const api = makeApi({ retry: vi.fn<ChatJobCommandApi['retry']>().mockRejectedValue(new Error('network')) })
  const { result, rerender } = renderHook(({ userId }) => useChatJobCommands(userId, api), {
    initialProps: { userId: 'user-1' as string | null },
  })

  await act(async () => {
    await result.current.retry('7001').catch(() => undefined)
  })

  rerender({ userId: 'user-2' })

  await act(async () => {
    await result.current.retry('7001').catch(() => undefined)
  })

  const [first, second] = api.retry.mock.calls
  expect(second?.[1]).not.toBe(first?.[1])
})
