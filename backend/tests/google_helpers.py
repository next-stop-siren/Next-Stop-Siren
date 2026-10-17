"""Fake Google claims and RSA signing helpers shared by verification tests."""

from datetime import datetime, timezone
import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
import jwt

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ACCESS_TOKEN = "fixture-google-access-token"
CODE = "fixture-google-code"
KID = "fixture-google-key"


def google_claims(transaction, settings):
    """실제 계정 정보 없이 필수 claim을 갖춘 Google 토큰 본문을 만든다."""
    return {
        "iss": settings.issuer,
        "sub": "fixture-google-subject",
        "aud": settings.client_id,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 3600,
        "nonce": transaction.nonce,
        "email": "fixture@example.test",
        "email_verified": True,
    }


def public_jwks(private_key, kid=KID):
    """서명에 사용한 키의 공개 부분만 JWKS 형식으로 내보낸다."""
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {"keys": [{**key, "kid": kid, "use": "sig", "alg": "RS256"}]}


def signed_token(claims, private_key, *, header=None):
    """잘못된 claim·헤더도 실제 RSA로 서명해 검증기가 거부하는지 시험한다."""

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    protected = {"alg": "RS256", "typ": "JWT", "kid": KID, **(header or {})}
    content = encode(json.dumps(protected).encode()) + b"." + encode(json.dumps(claims).encode())
    signature = private_key.sign(content, padding.PKCS1v15(), hashes.SHA256())
    return (content + b"." + encode(signature)).decode()
