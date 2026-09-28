import { useState } from 'react'
import { checkApi, type Check } from './lib/api'

type CheckState = { phase: 'idle' | 'loading' | 'success' | 'error'; message: string }

const initialState: CheckState = { phase: 'idle', message: '아직 확인하지 않았습니다.' }

function App() {
  const [health, setHealth] = useState<CheckState>(initialState)
  const [ready, setReady] = useState<CheckState>(initialState)

  async function runCheck(check: Check) {
    const update = check === 'health' ? setHealth : setReady
    update({ phase: 'loading', message: '확인 중…' })
    try {
      const status = await checkApi(check)
      update({ phase: 'success', message: status === 'ready' ? '데이터베이스에 연결되었습니다.' : 'API가 응답했습니다.' })
    } catch (error) {
      update({ phase: 'error', message: error instanceof TypeError ? 'API 서버에 연결할 수 없습니다.' : error instanceof Error ? error.message : '연결을 확인할 수 없습니다.' })
    }
  }

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">B7-1 · 개발 환경</p>
        <h1>기본 연결 확인</h1>
        <p className="intro">화면, API, 데이터베이스의 시작 상태를 확인합니다.</p>
      </header>
      <section className="checks" aria-label="연결 확인">
        <article className="card">
          <div className="card-heading"><span className="number">01</span><h2>API 응답</h2></div>
          <p>서버가 요청에 응답하는지 확인합니다.</p>
          <button onClick={() => void runCheck('health')} disabled={health.phase === 'loading'}>API 확인</button>
          <p className={`result ${health.phase}`} role="status">{health.message}</p>
        </article>
        <article className="card">
          <div className="card-heading"><span className="number">02</span><h2>데이터베이스 연결</h2></div>
          <p>API에서 PostgreSQL 연결과 간단한 쿼리를 확인합니다.</p>
          <button onClick={() => void runCheck('ready')} disabled={ready.phase === 'loading'}>DB 확인</button>
          <p className={`result ${ready.phase}`} role="status">{ready.message}</p>
        </article>
      </section>
      <footer>API 문서는 <a href="http://127.0.0.1:8000/docs">Swagger UI</a>에서 볼 수 있습니다.</footer>
    </main>
  )
}

export default App
