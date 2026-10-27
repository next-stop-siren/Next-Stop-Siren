"""Callback query removal operates on URL arguments before log formatting."""

import logging

import httpx
import pytest
from uvicorn.logging import AccessFormatter

from app.observability import GoogleCallbackQueryFilter

PATH = "/api/auth/google/callback"
UVICORN_FORMAT = '%s - "%s %s HTTP/%s" %d'
HTTPX_FORMAT = 'HTTP Request: %s %s "%s %d %s"'
QUERIES = [
    "code=fixture-secret&state=fixture-state",
    "note='demo'&code=fixture-secret&state=fixture-state",
    'note="demo"&code=fixture-secret&state=fixture-state',
    "note=%27demo%27&code=fixture-secret&state=fixture-state",
    "note=%22demo%22&code=fixture-secret&state=fixture-state",
    "note='\"demo\"'&code=fixture-secret&state=fixture-state",
    "note=with space&code=fixture-secret&state=fixture-state",
    "note=with\nnewline&code=fixture-secret&state=fixture-state",
]


def make_record(name, target):
    if name == "uvicorn.access":
        template = UVICORN_FORMAT
        args = ("127.0.0.1:1234", "GET", target, "1.1", 401)
    else:
        template = HTTPX_FORMAT
        # HTTPX rejects literal newlines; real URLs carry their percent-encoded form.
        args = ("GET", httpx.URL(target.replace("\n", "%0A")), "HTTP/1.1", 401, "Unauthorized")
    return logging.LogRecord(name, logging.INFO, __file__, 1, template, args, None)


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx"])
@pytest.mark.parametrize(
    "path",
    [
        PATH,
        PATH + "/",
        "/api/auth/google/%63allback",
        "/api/auth/google/start",
        "/api/auth/google/start/",
        "/api/auth/google/%73tart",
    ],
)
@pytest.mark.parametrize("query", QUERIES)
def test_entire_callback_query_is_removed_before_formatting(name, path, query):
    target = ("https://api.example.test" if name == "httpx" else "") + path + "?" + query
    record = make_record(name, target)
    template, original = record.msg, record.args
    assert GoogleCallbackQueryFilter().filter(record)
    assert record.msg == template and len(record.args) == 5
    position = 2 if name == "uvicorn.access" else 1
    assert record.args[position] == target.split("?", 1)[0]
    assert all(record.args[i] == original[i] for i in range(5) if i != position)
    formatted = (
        AccessFormatter("%(request_line)s %(status_code)s", use_colors=False).format(record)
        if name == "uvicorn.access"
        else record.getMessage()
    )
    assert "fixture-secret" not in formatted and "fixture-state" not in formatted
    assert "?" not in formatted and "note=" not in formatted
    assert "GET" in formatted and "401" in formatted


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx"])
@pytest.mark.parametrize("path", ["/api/health", "/api/auth/google/start-other", PATH + "-other"])
def test_unrelated_url_and_all_log_arguments_are_unchanged(name, path):
    target = ("https://api.example.test" if name == "httpx" else "") + path + "?note='demo'&value=keep"
    record = make_record(name, target)
    original = record.msg, record.args, record.getMessage()
    assert GoogleCallbackQueryFilter().filter(record)
    assert (record.msg, record.args, record.getMessage()) == original


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx"])
def test_callback_fragment_is_not_logged(name):
    target = ("https://api.example.test" if name == "httpx" else "") + PATH + "#fixture-secret"
    record = make_record(name, target)
    assert GoogleCallbackQueryFilter().filter(record)
    assert "fixture-secret" not in record.getMessage() and "#" not in record.getMessage()


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx"])
@pytest.mark.parametrize("path", [PATH, "/api/auth/google/%73tart"])
def test_unfamiliar_callback_log_format_is_suppressed(name, path):
    record = logging.LogRecord(
        name,
        logging.INFO,
        __file__,
        1,
        "Unexpected request: %s",
        (path + '?note="demo"&code=fixture-secret&state=fixture-state',),
        None,
    )
    assert GoogleCallbackQueryFilter().filter(record) is False


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx"])
def test_unfamiliar_noncallback_log_format_is_preserved(name):
    record = logging.LogRecord(name, logging.INFO, __file__, 1, "Connection closed", (), None)
    assert GoogleCallbackQueryFilter().filter(record)
    assert record.getMessage() == "Connection closed"
