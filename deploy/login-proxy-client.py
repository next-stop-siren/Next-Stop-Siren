"""Synthetic HTTPS login client, executed only inside disposable smoke clients."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import ssl
import sys


def login(task: dict) -> dict:
    payload = json.dumps(task["body"]).encode()
    connection = http.client.HTTPSConnection("nginx", 443, context=ssl._create_unverified_context(), timeout=30)
    try:
        connection.putrequest("POST", "/api/auth/login", skip_host=True)
        for name, value in (
            ("Host", "next-stop-siren.duckdns.org"),
            ("Origin", "https://next-stop-siren.duckdns.org"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
            *task.get("headers", []),
        ):
            connection.putheader(name, value)
        connection.endheaders(payload)
        response = connection.getresponse()
        body = response.read().decode()
        data = json.loads(body)
        cookies = [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
        return {
            "status": response.status,
            "code": data.get("error", {}).get("code"),
            "retry_after": response.getheader("Retry-After"),
            "auth_cookies": bool(cookies),
            "secure_cookies": bool(cookies) and all("Secure" in cookie and "HttpOnly" in cookie for cookie in cookies),
            "access_token_present": bool(data.get("access_token")),
            "password_echo": task["body"]["password"] in body,
        }
    finally:
        connection.close()


def main() -> None:
    config = json.load(sys.stdin)
    if config.get("concurrent"):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(login, config["tasks"]))
    else:
        results = [login(task) for task in config["tasks"]]
    print(json.dumps(results))


if __name__ == "__main__":
    main()
