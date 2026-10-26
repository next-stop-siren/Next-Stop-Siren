import { useEffect, useReducer, useRef } from 'react'

export interface ChatJobCommandApi {
  /** 가장 최근 실패 시도를 다시 시도한다. 같은 `retryKey`로 다시 보내면 서버가 기존 결과를 돌려준다. */
  retry: (jobId: string, retryKey: string) => Promise<unknown>
  skip: (jobId: string) => Promise<unknown>
  cancel: (jobId: string) => Promise<unknown>
}

export interface ChatJobCommandOptions {
  /**
   * 요청이 실패했을 때 그 작업을 서버 상태로 다시 읽도록 부른다. 서버가 이미 상태를 바꿔 거절했을 수 있고,
   * 그때 화면은 구독 이벤트를 놓쳐 뒤처져 있을 수 있기 때문이다. 이 함수가 실패해도 원래 오류는 그대로 전달한다.
   */
  onFailure?: (jobId: string) => void | Promise<void>
}

export interface ChatJobCommands {
  retry: (jobId: string) => Promise<void>
  /**
   * 이 작업의 재시도를 보냈지만 접수를 확인하지 못해 키를 보관 중인가. 다시 누르면 같은 키로 확인하는 것이라
   * 한도 0·10초 간격이어도 버튼을 막지 않는다.
   */
  hasUnconfirmedRetry: (jobId: string) => boolean
  skip: (jobId: string) => Promise<void>
  cancel: (jobId: string) => Promise<void>
}

/**
 * 재시도·건너뛰기·취소를 서버에 보내는 명령.
 *
 * - 재시도의 `retry_key`는 버튼 시도 하나에 하나다. 응답을 못 받았거나 거절돼 실패하면 키를 남겨 두고,
 *   다시 누르면 같은 키로 보낸다. 접수가 확인되면 키를 버린다(다음 재시도는 새 시도라 새 키를 쓴다).
 * - 같은 작업·같은 동작이 진행 중일 때 다시 호출하면 새로 보내지 않고 진행 중인 요청을 기다린다.
 * - 요청이 실패하면 `onFailure`로 그 작업을 다시 읽게 해서, 거절 이유가 된 최신 서버 상태로 화면을 맞춘다.
 * - 사용자가 바뀌거나 로그아웃하면 보관한 키와 진행 중 표시를 모두 버린다.
 * - 결과 화면은 이 함수의 반환값이 아니라 구독 이벤트로 바뀐다. 성공해도 화면 상태를 여기서 바꾸지 않는다.
 */
export function useChatJobCommands(
  userId: string | null,
  api: ChatJobCommandApi,
  options: ChatJobCommandOptions = {},
): ChatJobCommands {
  const apiRef = useRef(api)
  const optionsRef = useRef(options)
  useEffect(() => {
    apiRef.current = api
    optionsRef.current = options
  })

  const retryKeysRef = useRef(new Map<string, string>())
  const inFlightRef = useRef(new Map<string, Promise<void>>())
  // 보관한 재시도 키가 바뀌면 버튼의 비활성 여부를 다시 계산하도록 다시 그린다.
  const [, keysChanged] = useReducer((count: number) => count + 1, 0)

  useEffect(() => {
    // 사용자가 바뀌거나 화면이 사라질 때 이전 사용자의 키와 진행 중 표시를 버린다.
    return () => {
      retryKeysRef.current.clear()
      inFlightRef.current.clear()
    }
  }, [userId])

  function once(name: string, jobId: string, send: () => Promise<void>): Promise<void> {
    const id = `${name}:${jobId}`
    const running = inFlightRef.current.get(id)
    if (running) return running

    const inFlight = inFlightRef.current

    const promise = send()
      .catch((error: unknown) => {
        // 화면을 최신 서버 상태로 맞추는 일이 실패해도 원래 오류를 가리지 않는다.
        void Promise.resolve()
          .then(() => optionsRef.current.onFailure?.(jobId))
          .catch(() => undefined)

        throw error
      })
      .finally(() => {
        if (inFlight.get(id) === promise) inFlight.delete(id)
      })
    inFlight.set(id, promise)

    return promise
  }

  return {
    retry: (jobId) =>
      once('retry', jobId, async () => {
        const keys = retryKeysRef.current
        const key = keys.get(jobId) ?? crypto.randomUUID()
        keys.set(jobId, key)

        try {
          await apiRef.current.retry(jobId, key)
        } catch (error) {
          keysChanged()
          throw error
        }

        // 접수가 확인됐다. 이 시도의 키는 더 쓰지 않는다.
        if (keys.get(jobId) === key) {
          keys.delete(jobId)
          keysChanged()
        }
      }),

    hasUnconfirmedRetry: (jobId) => retryKeysRef.current.has(jobId),

    skip: (jobId) =>
      once('skip', jobId, async () => {
        await apiRef.current.skip(jobId)
      }),

    cancel: (jobId) =>
      once('cancel', jobId, async () => {
        await apiRef.current.cancel(jobId)
      }),
  }
}
