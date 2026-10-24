"""Session refresh, token persistence, and secret redaction."""

from __future__ import annotations

import json
import logging

from custom_components.eero.api import EeroAPI, EeroAuthException, EeroException
from custom_components.eero.api.const import METHOD_GET, METHOD_POST
from custom_components.eero.const import CONF_USER_TOKEN
from custom_components.eero import create_eero_api

from pytest_homeassistant_custom_component.common import MockConfigEntry


class FakeResponse:
    """Minimal response object for EeroAPI.parse_response."""

    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self.ok = status < 400
        self.reason = "OK" if self.ok else "error"
        self.text = json.dumps(payload)
        self.url = "https://api-user.e2ro.com/test"


class FakeSession:
    """Records requests and returns scripted responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._next("DELETE", url, **kwargs)


def _session_error(status: int = 401) -> FakeResponse:
    return FakeResponse(
        status,
        {
            "meta": {"code": 401, "error": "error.session.invalid"},
            "data": {"user_token": "super-secret-token"},
        },
    )


def test_login_and_verify_do_not_log_secrets(caplog) -> None:
    """Logs may name the action, but not the account, code, or session token."""
    api = EeroAPI()
    api.session = FakeSession(
        [
            FakeResponse(
                200,
                {"meta": {"code": 200}, "data": {"user_token": "super-secret-token"}},
            )
        ]
    )
    with caplog.at_level(logging.DEBUG):
        api.login("secretuser@example.com")
    assert api.user_token == "super-secret-token"
    assert "secretuser@example.com" not in caplog.text
    assert "super-secret-token" not in caplog.text
    assert "s***@example.com" in caplog.text

    caplog.clear()
    api.session = FakeSession(
        [FakeResponse(200, {"meta": {"code": 200}, "data": {"log_id": "abc", "name": "A"}})]
    )
    with caplog.at_level(logging.DEBUG):
        api.login_verify("654321")
    assert "654321" not in caplog.text
    assert "super-secret-token" not in caplog.text


def test_error_payload_redacts_token(caplog) -> None:
    """A failed response body is logged without the session token."""
    api = EeroAPI(user_token="super-secret-token")
    api.session = FakeSession(
        [
            FakeResponse(
                500,
                {
                    "meta": {"code": 500, "error": "error.server"},
                    "data": {"user_token": "super-secret-token", "login": "secretuser@example.com"},
                },
            )
        ]
    )
    with caplog.at_level(logging.DEBUG):
        try:
            api.call(METHOD_GET, "/2.2/account")
        except EeroException:
            pass
    assert "super-secret-token" not in caplog.text
    assert "secretuser@example.com" not in caplog.text


def test_rejected_session_refreshes_once_and_retries_the_read() -> None:
    """A rejected session refreshes once, then the original GET is tried once."""
    api = EeroAPI(user_token="old-token")
    api.session = FakeSession(
        [
            _session_error(),
            FakeResponse(
                200,
                {"meta": {"code": 200}, "data": {"user_token": "renewed-token"}},
            ),
            FakeResponse(200, {"meta": {"code": 200}, "data": {"name": "Home"}}),
        ]
    )

    assert api.call(METHOD_GET, "/2.2/account") == {"name": "Home"}

    methods = [call[0] for call in api.session.calls]
    assert methods == ["GET", "POST", "GET"]
    assert api.session.calls[1][1].endswith("/2.2/login/refresh")
    assert api.session.calls[2][2]["cookies"] == {"s": "renewed-token"}
    assert api.user_token == "renewed-token"


def test_failed_refresh_does_not_retry(caplog) -> None:
    """When refresh fails, the original request is not sent again."""
    notified = []
    api = EeroAPI(user_token="old-token", on_auth_failed=lambda: notified.append("auth"))
    api.session = FakeSession([_session_error(), _session_error()])

    with caplog.at_level(logging.DEBUG):
        try:
            api.call(METHOD_GET, "/2.2/account")
            raise AssertionError("expected auth failure")
        except EeroAuthException:
            pass

    assert [call[0] for call in api.session.calls] == ["GET", "POST"]
    assert notified == ["auth"]
    assert "super-secret-token" not in caplog.text


def test_write_is_not_retried_when_the_session_is_rejected() -> None:
    """Writes pass allow_refresh=False and are not repeated after a 401."""
    api = EeroAPI(user_token="old-token")
    api.session = FakeSession([_session_error()])

    try:
        api.call(
            METHOD_POST,
            "/2.2/networks/net-1/reservations",
            json={"ip": "192.168.4.50", "mac": "aa:bb:cc:dd:ee:ff", "description": "Printer"},
            allow_refresh=False,
        )
        raise AssertionError("expected auth failure")
    except EeroAuthException:
        pass

    assert len(api.session.calls) == 1
    assert api.session.calls[0][0] == "POST"


async def test_refreshed_token_is_saved_on_the_config_entry(hass) -> None:
    """A renewed session token is written back to the config entry."""
    entry = MockConfigEntry(
        domain="eero",
        entry_id="entry-1",
        unique_id="log-1",
        data={CONF_USER_TOKEN: "old-token", "login": "user@example.com"},
        title="Home",
    )
    entry.add_to_hass(hass)
    api = create_eero_api(hass, entry, user_token="old-token")
    api.session = FakeSession(
        [
            _session_error(),
            FakeResponse(
                200,
                {"meta": {"code": 200}, "data": {"user_token": "renewed-token"}},
            ),
            FakeResponse(200, {"meta": {"code": 200}, "data": {"name": "Home"}}),
        ]
    )

    await hass.async_add_executor_job(api.call, METHOD_GET, "/2.2/account")
    await hass.async_block_till_done()

    assert entry.entry_id == "entry-1"
    assert entry.data[CONF_USER_TOKEN] == "renewed-token"
    assert api.user_token == "renewed-token"
