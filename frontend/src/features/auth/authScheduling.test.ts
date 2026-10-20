import { afterEach, expect, test, vi } from 'vitest'
import { createExpiryTimers } from './expiryTimers'
import { createMutationQueue } from './mutationQueue'
afterEach(() => {
  vi.useRealTimers()
})

test('expiry timers replace previous callbacks, honor absolute deadlines, and clear both', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
  const timers = createExpiryTimers()
  const oldSoft = vi.fn(),
    oldHard = vi.fn(),
    soft = vi.fn(),
    hard = vi.fn()
  timers.schedule(50_000, oldSoft, oldHard)
  await vi.advanceTimersByTimeAsync(5000)
  timers.schedule(40_000, soft, hard)
  await vi.advanceTimersByTimeAsync(4999)
  expect(soft).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(soft).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(29_999)
  expect(hard).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(hard).toHaveBeenCalledTimes(1)
  expect(oldSoft).not.toHaveBeenCalled()
  expect(oldHard).not.toHaveBeenCalled()
  timers.schedule(90_000, oldSoft, oldHard)
  timers.clear()
  await vi.advanceTimersByTimeAsync(100_000)
  expect(oldSoft).not.toHaveBeenCalled()
  expect(oldHard).not.toHaveBeenCalled()
})

test('separate timer owners cannot cancel each other', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
  const a = createExpiryTimers(),
    b = createExpiryTimers(),
    fired = vi.fn()
  a.schedule(31_000, vi.fn(), vi.fn())
  b.schedule(31_000, fired, fired)
  a.clear()
  await vi.advanceTimersByTimeAsync(31_000)
  expect(fired).toHaveBeenCalledTimes(2)
})

test('queue waits for completion, recovers after rejection, and instances progress independently', async () => {
  const enqueue = createMutationQueue(),
    independent = createMutationQueue()
  let reject!: (e: Error) => void
  const gate = new Promise<void>((_, r) => {
    reject = r
  })
  const events: string[] = []
  const first = enqueue(async () => {
    events.push('A starts')
    await gate
  })
  const rejected = expect(first).rejects.toThrow('A fails')
  const second = enqueue(async () => {
    events.push('B starts')
    return 'B succeeds'
  })
  await independent(async () => {
    events.push('other owner')
  })
  expect(events).toEqual(['A starts', 'other owner'])
  reject(new Error('A fails'))
  await rejected
  await expect(second).resolves.toBe('B succeeds')
  expect(events).toEqual(['A starts', 'other owner', 'B starts'])
})
