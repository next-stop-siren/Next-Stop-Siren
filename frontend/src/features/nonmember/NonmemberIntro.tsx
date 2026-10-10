type NonmemberIntroProps = {
  onAuthRequired: () => void
}

const exampleQuestion = '어린이 보호구역에서는 주정차를 어떻게 해야 하나요?'

const exampleAnswer =
  '어린이 보호구역에서는 주정차가 제한될 수 있습니다. 구체적인 적용 기준은 해당 장소의 표지와 관련 법령을 확인해야 합니다.'

const popularQuestions = [
  '주정차 금지구역은 어디인가요?',
  '제한속도를 위반하면 어떤 처벌을 받나요?',
  '횡단보도에 주차하면 어떻게 되나요?',
  '교통사고가 발생하면 어떻게 대처해야 하나요?',
]

function NonmemberIntro({ onAuthRequired }: NonmemberIntroProps) {
  return (
    <section className="intro-page" aria-labelledby="service-title">
      <header className="intro-header">
        <p className="eyebrow">교통법 안내 서비스</p>
        <h1 id="service-title">
          복잡한 교통법,
          <br />
          쉽게 확인해 보세요.
        </h1>
        <p className="intro-description">교통법에 관한 궁금증을 질문하고 필요한 정보를 확인할 수 있어요.</p>
      </header>

      <section className="chat-preview" aria-label="질문과 답변 예시">
        <div className="user-message">
          <p>{exampleQuestion}</p>
        </div>

        <div className="ai-message">
          <p className="answer-label">AI 답변 예시</p>
          <p>{exampleAnswer}</p>
        </div>
      </section>

      <section className="popular-questions" aria-labelledby="popular-title">
        <h2 id="popular-title">이런 질문도 많이 물어봐요</h2>
        <ul>
          {popularQuestions.map((question) => (
            <li key={question}>{question}</li>
          ))}
        </ul>
      </section>

      <div className="prompt-area">
        <label className="visually-hidden" htmlFor="guest-prompt">
          교통법 질문 입력
        </label>
        <input
          id="guest-prompt"
          type="text"
          placeholder="교통법에 대해 궁금한 내용을 물어보세요..."
          readOnly
          onClick={onAuthRequired}
          onFocus={onAuthRequired}
          aria-label="질문하려면 로그인 또는 회원가입이 필요합니다"
        />
        <button type="button" className="send-button" onClick={onAuthRequired} aria-label="로그인하고 질문하기">
          ↑
        </button>
      </div>
    </section>
  )
}

export default NonmemberIntro
