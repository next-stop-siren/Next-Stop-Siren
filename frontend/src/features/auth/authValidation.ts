// 브라우저 폼의 입력 검증. 문자 수는 UTF-16 길이가 아닌 Unicode code point 기준이다.
export function validateEmail(rawEmail: string): string | null {
  const email = rawEmail.trim()
  if (email.length === 0) return '이메일을 입력해 주세요.'
  if ([...email].length > 320) return '이메일이 너무 깁니다.'
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return '올바른 이메일 형식이 아닙니다.'
  }
  return null
}

export function validatePassword(password: string): string | null {
  const codePointLength = [...password].length

  if (codePointLength < 12 || codePointLength > 128) {
    return '비밀번호는 12자 이상 128자 이하여야 합니다.'
  }

  const byteLength = new TextEncoder().encode(password).length
  if (byteLength > 1024) return '비밀번호가 너무 깁니다.'

  return null
}
