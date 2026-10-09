import { useState } from 'react'

const exampleQuestion = '어린이 보호구역에서는 주정차를 어떻게 해야 하나요?'

const exampleAnswer =
  '어린이 보호구역에서는 일반 도로와 다른 주정차 관련 규정이 적용될 수 있습니다. 실제 적용 여부는 도로의 표지와 현재 시행되는 관련 규정을 확인해야 합니다.'

function NonmemberIntro() {
  const [showExample, setShowExample] = useState(true)

  return (
    <section aria-labelledby="service-title">
      <header>
        <p>교통법 안내 서비스</p>
        <h1 id="service-title">교통법이 궁금할 때 쉽게 확인해 보세요.</h1>
        <p>교통법과 관련된 궁금한 내용을 질문하고 필요한 정보를 확인할 수 있는 서비스입니다.</p>
      </header>

      <section aria-labelledby="example-title">
        <h2 id="example-title">이런 질문을 할 수 있어요</h2>

        <button type="button" onClick={() => setShowExample((current) => !current)} aria-expanded={showExample}>
          {showExample ? '예시 숨기기' : '예시 보기'}
        </button>

        {showExample && (
          <article>
            <h3>질문</h3>
            <p>{exampleQuestion}</p>

            <h3>답변 예시</h3>
            <p>{exampleAnswer}</p>
          </article>
        )}
      </section>
    </section>
  )
}

export default NonmemberIntro
