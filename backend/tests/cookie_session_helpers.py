"""Seed a browser-style host-only cookie jar using the production cookie builder."""

from fastapi import Response
import httpx

from app.api.cookies import set_auth_cookies


def seed_auth_cookies(client, refresh, settings):
    response = Response()
    set_auth_cookies(response, refresh, settings)
    client.cookies.extract_cookies(
        httpx.Response(
            200,
            headers=response.raw_headers,
            request=httpx.Request("GET", "http://127.0.0.1:8000/api/auth/login"),
        )
    )
