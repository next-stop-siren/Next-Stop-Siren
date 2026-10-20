// 신원과 generation은 Provider가 소유한다. 이 객체는 deadline과 timer만 관리한다.
// Provider마다 독립 객체를 만들며 reschedule/clear는 두 timer를 함께 취소한다.
export function createExpiryTimers() {
  let soft: ReturnType<typeof setTimeout> | null = null
  let hard: ReturnType<typeof setTimeout> | null = null

  function clear() {
    if (soft !== null) clearTimeout(soft)
    if (hard !== null) clearTimeout(hard)
    soft = null
    hard = null
  }

  function schedule(expiresAtMs: number, onSoft: () => void, onHard: () => void) {
    clear()
    soft = setTimeout(
      () => {
        soft = null
        onSoft()
      },
      Math.max(expiresAtMs - 30_000 - Date.now(), 0),
    )
    hard = setTimeout(
      () => {
        hard = null
        onHard()
      },
      Math.max(expiresAtMs - Date.now(), 0),
    )
  }

  return { schedule, clear }
}
