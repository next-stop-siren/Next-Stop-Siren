import { useEffect, useRef } from 'react'

export interface ChatJobCommandApi {
  /** 가장 최근 실패 시도를 다시 시도한다. 같은 `retryKey`로 다시 보내면 서버가 기존 결과를 돌려준다. */
  retry: (jobId: string, retryKey: string) => Promise<unknown>
  skip: (jobId: string) => Promise<unknown>
  cancel: (jobId: string) => Promise<unknown>
}

export interface ChatJobCommands {
  retry: (jobId: string) => Promise<void>
  skip: (jobId: string) => Promise<void>
  cancel: (jobId: string) => Promise<void>
}

/**
 * 재시도·건너뛰기·취소를 서버에 보내는 명령.
 *
 * - 재시도의 `retry_key`는 버튼 시도 하나에 하나다. 응답을 못 받았거나 거절돼 실패하면 키를 남겨 두고,
 *   다시 누르면 같은 키로 보낸다. 접수가 확인되면 키를 버린다(다음 재시도는 새 시도라 새 키를 쓴다).
 * - 같은 작업·같은 동작이 진행 중일 때 다시 호출하면 새로 보내지 않고 진행 중인 요청을 기다린다.
 * - 사용자가 바뀌거나 로그아웃하면 보관한 키와 진행 중 표시를 모두 버린다.
 * - 결과 화면은 이 함수의 반환값이 아니라 구독 이벤트로 바뀐다. 성공해도 화면 상태를 여기서 바꾸지 않는다.
 */
export function useChatJobCommands(userId: string | null, api: ChatJobCommandApi): ChatJobCommands {
  const apiRef = useRef(api)
  useEffect(() => {
    apiRef.current = api
  })

  const retryKeysRef = useRef(new Map<string, string>())
  const inFlightRef = useRef(new Map<string, Promise<void>>())

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

    const promise = send().finally(() => {
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

        await apiRef.current.retry(jobId, key)

        // 접수가 확인됐다. 이 시도의 키는 더 쓰지 않는다.
        if (keys.get(jobId) === key) keys.delete(jobId)
      }),

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
