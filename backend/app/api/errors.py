"""The common error body of docs/api.md for expected failures and invalid input."""
#FastAPI가 검사하고 예외를 보내면, 여기서 예외의 응답 모양을 바꾸는 것
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An expected failure with a stable code and a message that is safe to display."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        #class의 Exception 부르는 것 built-in 함수 
        self.status_code = status_code
        self.code = code
        self.message = message


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "로그인이 필요합니다.")
#auth.py에서 따로 보내줌!

def not_found() -> ApiError:
    """One answer for a missing resource and for another user's resource."""
    return ApiError(404, "not_found", "찾을 수 없습니다.")


def error_response(status_code: int, code: str, message: str, fields: dict[str, str] | None = None) -> JSONResponse:
    error = {"code": code, "message": message, "fields": fields or {}, "trace_id": uuid4().hex}
    return JSONResponse(status_code=status_code, content={"error": error})
#이미 오류가 난 결과를 스키마 형태로 변환 -- DB형태로 바꾸는 것

def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # The last location part is the field name; a malformed or missing body has none.
        fields = {}
        for error in exc.errors():
            name = error["loc"][-1]
            fields[name if isinstance(name, str) else "body"] = "값을 확인해 주세요."
        return error_response(422, "validation_error", "요청을 확인해 주세요.", fields)
# 에러 이유를 fields에 넣는 거
# FastAPI가 RequestValidationError를 보내줌! 
# exc.errors()는 FastAPI가 보내준 틀린부분.. 여러 개일 수 있어서 for문으로
# ["loc"][-1]인 이유
# exc.errors() =
 # [ {'loc': ('query', 'limit'),     'msg': 'Input should be greater than or equal to 1'},
  #  {'loc': ('query', 'before_id'), 'msg': 'Value error, ID must be a decimal string'} ]

#→ fields = {"limit": "값을 확인해 주세요.", "before_id": "값을 확인해 주세요."}