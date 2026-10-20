// 한 Provider 안에서 Promise가 끝날 때까지 다음 쿠키 변경 요청 전송을 기다린다.
// 다른 탭이나 클라이언트 timeout 뒤 서버 작업의 완료 순서를 보장하지 않는다.
export function createMutationQueue() {
  let tail: Promise<unknown> = Promise.resolve()
  return function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const run = tail.then(operation)
    // 호출자에게는 원래 실패를 돌려주되 다음 작업은 진행시킨다.
    tail = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }
}
