import NonmemberIntro from './features/nonmember/NonmemberIntro'
import Sidebar from './features/layout/Sidebar'

function App() {
  return (
    <div className="app-layout">
      <Sidebar />

      <main className="main-content">
        <NonmemberIntro />
      </main>
    </div>
  )
}

export default App
