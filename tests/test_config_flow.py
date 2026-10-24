"""Reconfigure keeps the config entry, and reauthentication replaces the token."""

from __future__ import annotations

import logging
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr

from custom_components.eero.const import (
    CONF_CODE,
    CONF_LOGIN,
    CONF_NETWORKS,
    CONF_RESOURCES,
    CONF_USER_TOKEN,
    DOMAIN,
)


class FakeFlowAPI:
    """Login client that never touches the network."""

    def __init__(self, *args, **kwargs) -> None:
        self.user_token = None
        self.calls: list[tuple] = []

    def login(self, login):
        self.calls.append(("login", login))
        self.user_token = "token-from-login"
        return {"user_token": "token-from-login"}

    def login_verify(self, code):
        self.calls.append(("verify", code))
        if code == "000000":
            from custom_components.eero.api import EeroException

            raise EeroException(code=401, error="invalid_code", message="invalid code")
        return {"log_id": "New-Account", "name": "New Name"}

    def update(self):
        raise AssertionError("account linking must not reload network resources")


def _entry(entry_id="entry-keep", unique_id="old-account", login="old@example.com"):
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        unique_id=unique_id,
        title=f"Old ({login})",
        data={
            CONF_NAME: "Old",
            CONF_LOGIN: login,
            CONF_USER_TOKEN: "old-token",
            CONF_NETWORKS: ["net-1"],
            CONF_RESOURCES: {"net-1": {"eeros": ["eero-1"]}},
        },
    )


async def _finish(hass, flow_id, code="123456"):
    with patch(
        "custom_components.eero.config_flow.EeroAPI",
        FakeFlowAPI,
    ), patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CODE: code}
        )
        await hass.async_block_till_done()
    return result


async def test_reconfigure_keeps_entry_id_devices_and_resources(hass, caplog) -> None:
    """Switching accounts updates credentials and leaves the entry id in place."""
    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "net-1")},
        name="Ohana",
    )

    with caplog.at_level(logging.DEBUG), patch(
        "custom_components.eero.config_flow.EeroAPI",
        FakeFlowAPI,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
        )
        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure_login"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOGIN: "secretuser@example.com"}
        )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure_verify"

    result = await _finish(hass, result["flow_id"], "654321")

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.entry_id == "entry-keep"
    assert entry.unique_id == "new-account"
    assert entry.data[CONF_LOGIN] == "secretuser@example.com"
    assert entry.data[CONF_USER_TOKEN] == "token-from-login"
    assert entry.data[CONF_NAME] == "New Name"
    assert entry.data[CONF_NETWORKS] == ["net-1"]
    assert entry.data[CONF_RESOURCES] == {"net-1": {"eeros": ["eero-1"]}}
    assert entry.title == "New Name (secretuser@example.com)"
    assert dr.async_get(hass).async_get(device.id) is device
    assert device.config_entries == {entry.entry_id}
    assert "secretuser@example.com" not in caplog.text
    assert "654321" not in caplog.text
    assert "token-from-login" not in caplog.text


async def test_reconfigure_refuses_an_account_already_in_use(hass) -> None:
    """Two entries cannot represent the same Eero account."""
    entry = _entry()
    entry.add_to_hass(hass)
    other = _entry(entry_id="entry-other", unique_id="new-account", login="other@example.com")
    other.add_to_hass(hass)

    with patch("custom_components.eero.config_flow.EeroAPI", FakeFlowAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOGIN: "secretuser@example.com"}
        )
    result = await _finish(hass, result["flow_id"])

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert entry.entry_id == "entry-keep"
    assert entry.unique_id == "old-account"
    assert entry.data[CONF_USER_TOKEN] == "old-token"
    assert entry.data[CONF_LOGIN] == "old@example.com"
    assert other.entry_id == "entry-other"


async def test_reauth_replaces_the_token_and_keeps_the_entry(hass) -> None:
    """Reauthentication stores a new session token on the same entry."""
    entry = _entry(unique_id="new-account", login="secretuser@example.com")
    entry.add_to_hass(hass)

    with patch("custom_components.eero.config_flow.EeroAPI", FakeFlowAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        assert result["step_id"] == "reauth_login"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOGIN: "secretuser@example.com"}
        )
        assert result["step_id"] == "reauth_verify"
    result = await _finish(hass, result["flow_id"], "654321")

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.entry_id == "entry-keep"
    assert entry.unique_id == "new-account"
    assert entry.data[CONF_USER_TOKEN] == "token-from-login"
    assert entry.data[CONF_NETWORKS] == ["net-1"]


async def test_user_flow_still_refuses_a_duplicate_account(hass) -> None:
    """The initial setup flow still aborts when the account is already configured."""
    entry = _entry(unique_id="new-account")
    entry.add_to_hass(hass)

    with patch("custom_components.eero.config_flow.EeroAPI", FakeFlowAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_LOGIN: "secretuser@example.com"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CODE: "654321"}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
