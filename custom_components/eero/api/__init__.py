"""Eero API."""

from __future__ import annotations

from collections.abc import Callable
import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import aiofiles
from dateutil import relativedelta
import requests

from .account import EeroAccount
from .const import (
    ACTIVITY_MAP,
    API_ENDPOINT,
    CADENCE_DAILY,
    CADENCE_HOURLY,
    EERO_LOGO_ICON,
    METHOD_DELETE,
    METHOD_GET,
    METHOD_POST,
    METHOD_PUT,
    PERIOD_DAY,
    PERIOD_MONTH,
    PERIOD_WEEK,
    RESOURCE_MAP,
    URL_ACCOUNT,
)
from .util import backup_access_point_ok, premium_ok

_LOGGER = logging.getLogger(__name__)

_QUOTED_SECRET = re.compile(
    r'("(?:user_token|login|code)"\s*:\s*")[^"]*"',
    re.IGNORECASE,
)


def redact_login(login: str | int) -> str:
    """Return a log-safe hint for an email address or phone number."""
    text = str(login).strip()
    if "@" in text:
        local, _, domain = text.partition("@")
        prefix = local[:1]
        return f"{prefix}***@{domain}"
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) >= 2:
        return f"***{digits[-2:]}"
    return "***"


def redact_secrets(value: str | None, token: str | None = None) -> str | None:
    """Remove session tokens, logins, and verification codes from text."""
    if not value:
        return value
    redacted = _QUOTED_SECRET.sub(r'\1***"', value)
    if token:
        redacted = redacted.replace(token, "***")
    return redacted


def redact_data(value: Any, token: str | None = None) -> Any:
    """Return a copy of API data safe to write to disk."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in {"user_token", "login", "code"} and isinstance(item, str):
                redacted[key] = "***"
            else:
                redacted[key] = redact_data(item, token)
        return redacted
    if isinstance(value, list):
        return [redact_data(item, token) for item in value]
    if isinstance(value, str):
        return redact_secrets(value, token)
    return value


class EeroException(Exception):
    """EeroException."""

    def __init__(
        self,
        code: int | None = None,
        error: str | None = None,
        message: str | None = None,
        payload: str | None = None,
        server_time: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__()
        self.code = code
        self.error = error
        self.message = message
        self.payload = payload
        self.server_time = server_time
        exception = self.__class__.__name__
        if self.code:
            exception = f"{exception}\n- Code: {self.code}"
        if self.error:
            exception = f"{exception}\n- Error: {self.error}"
        if self.message:
            exception = f"{exception}\n- Message: {self.message}"
        if self.payload:
            exception = f"{exception}\n- Payload: {redact_secrets(self.payload)}"
        if self.server_time:
            exception = f"{exception}\n- Server time: {self.server_time}"
        _LOGGER.warning(exception)


class EeroAuthException(EeroException):
    """The session was rejected and could not be refreshed."""


class EeroAPI:
    """EeroAPI."""

    def __init__(
        self,
        save_location: str | None = None,
        show_eero_logo: dict[str, bool] | None = None,
        user_token: str | None = None,
        on_user_token: Callable[[str], None] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        """Initialize."""
        self.data = EeroAccount(self, {})
        self.default_qr_code: bytes | None = None
        self.save_location = save_location
        self.session = requests.Session()
        self.show_eero_logo = show_eero_logo
        self.user_token = user_token
        self._on_user_token = on_user_token
        self._on_auth_failed = on_auth_failed
        self._refresh_in_progress = False
        if self.show_eero_logo is None:
            self.show_eero_logo = {}

    def _set_user_token(self, token: str | None) -> None:
        """Store a session token and tell the integration when it changes."""
        if not token or token == self.user_token:
            return
        self.user_token = token
        if self._on_user_token is not None:
            self._on_user_token(token)

    def _notify_auth_failed(self) -> None:
        """Ask Home Assistant to start reauthentication."""
        if self._on_auth_failed is not None:
            self._on_auth_failed()

    @staticmethod
    def _session_rejected(text: Any) -> bool:
        """Return True when the payload says the session cookie was rejected."""
        if not isinstance(text, dict):
            return False
        meta = text.get("meta")
        if not isinstance(meta, dict):
            return False
        return meta.get("code") == 401 and meta.get("error") in {
            "error.session.invalid",
            "error.session.refresh",
        }

    def _http_error(self, response: requests.Response) -> EeroException:
        """Build an exception for a non-success response."""
        return EeroException(
            code=response.status_code,
            error=response.reason,
            message=f"Bad response received from URL: {response.url}",
            payload=redact_secrets(response.text, self.user_token),
        )

    @property
    def cookie(self) -> dict:
        """Cookie."""
        if self.user_token:
            return {"s": self.user_token}
        return {}

    def call(
        self, method: str, url: str, **kwargs
    ) -> dict[str, Any] | list[Any] | None:
        """Call.

        ``allow_refresh`` is consumed here and is not sent to the API.
        Writes pass ``allow_refresh=False`` so a failed request is not repeated.
        """
        allow_refresh = kwargs.pop("allow_refresh", True)
        if method not in [METHOD_DELETE, METHOD_GET, METHOD_POST, METHOD_PUT]:
            return None
        _LOGGER.debug("Calling API with method: %s and URL: %s", method, url)
        requester = {
            METHOD_DELETE: self.session.delete,
            METHOD_GET: self.session.get,
            METHOD_POST: self.session.post,
            METHOD_PUT: self.session.put,
        }[method]
        response = self.parse_response(
            lambda: requester(
                url=f"{API_ENDPOINT}{url}", cookies=self.cookie, **kwargs
            ),
            allow_refresh=allow_refresh,
        )
        self.save_response(
            response=redact_data(response, self.user_token),
            name=url,
        )
        return response

    def define_period(self, period: str, timezone: str) -> tuple:
        """Define period."""
        start, end, cadence = None, None, None
        now = datetime.datetime.now(tz=ZoneInfo(timezone))
        if period == PERIOD_DAY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = (
                start
                + relativedelta.relativedelta(days=1)
                - datetime.timedelta(seconds=1)
            )
            cadence = CADENCE_HOURLY
        elif period == PERIOD_WEEK:
            start = now - relativedelta.relativedelta(days=now.weekday() + 1)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = (
                start
                + relativedelta.relativedelta(weeks=1)
                - datetime.timedelta(seconds=1)
            )
            cadence = CADENCE_DAILY
        elif period == PERIOD_MONTH:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = (
                start
                + relativedelta.relativedelta(months=1)
                - datetime.timedelta(seconds=1)
            )
            cadence = CADENCE_DAILY
        else:
            return (start, end, cadence)
        start = f"{start.astimezone(datetime.UTC).replace(tzinfo=None).isoformat()}Z"
        end = f"{end.astimezone(datetime.UTC).replace(tzinfo=None).isoformat()}Z"
        return (start, end, cadence)

    async def generate_default_qr_code(self) -> None:
        """Generate default QR code."""
        async with aiofiles.open(EERO_LOGO_ICON, "rb") as file:
            self.default_qr_code = await file.read()

    def get_release_notes(self, url: str) -> dict[str, Any] | None:
        """Get release notes."""
        if url:
            response = self.timeout(lambda: self.session.get(url=url))
            if not response.ok:
                raise EeroException(
                    code=response.status_code,
                    error=response.reason,
                    message=f"Unable to get release notes from URL: {url}",
                    payload=response.text,
                )
            text = self.decode_json(response)
            self.save_response(response=text, name="release_notes")
            return text
        return None

    def login(self, login: str | int) -> dict[str, Any]:
        """Login."""
        _LOGGER.debug("Starting login for %s", redact_login(login))
        response = self.call(
            method=METHOD_POST,
            url="/2.2/login",
            json={"login": login},
            allow_refresh=False,
        )
        self._set_user_token(response["user_token"])
        return response

    def login_refresh(self) -> dict[str, Any]:
        """Login refresh."""
        _LOGGER.debug("Refreshing session")
        response = self.call(
            method=METHOD_POST,
            url="/2.2/login/refresh",
            allow_refresh=False,
        )
        token = response.get("user_token") if isinstance(response, dict) else None
        if not token:
            raise EeroAuthException(message="Session refresh did not return a token")
        self._set_user_token(token)
        return response

    def login_verify(self, code: str) -> dict[str, Any]:
        """Login verify."""
        _LOGGER.debug("Verifying login code")
        response = self.call(
            method=METHOD_POST,
            url="/2.2/login/verify",
            json={"code": code},
            allow_refresh=False,
        )
        if isinstance(response, dict) and response.get("user_token"):
            self._set_user_token(response["user_token"])
        return response

    def decode_json(self, response: requests.Response) -> dict[str, Any]:
        """Decode JSON."""
        try:
            return json.loads(response.text)
        except json.decoder.JSONDecodeError as exception:
            raise EeroException(
                code=response.status_code,
                error=response.reason,
                message="Unable to decode JSON",
                payload=redact_secrets(response.text, self.user_token),
            ) from exception

    def parse_response(
        self, function: Callable, allow_refresh: bool = True
    ) -> dict[str, Any] | list[Any] | None:
        """Parse response.

        A rejected session is refreshed once and the original request is retried
        once. The refresh request itself, and callers that pass
        ``allow_refresh=False``, are never retried.
        """
        response = self.timeout(function)
        if not response.ok:
            text = self.decode_json(response)
            if self._session_rejected(text):
                _LOGGER.debug("Session has expired and is invalid")
                if allow_refresh and not self._refresh_in_progress:
                    self._refresh_in_progress = True
                    try:
                        self.login_refresh()
                    except EeroException as err:
                        self._notify_auth_failed()
                        if isinstance(err, EeroAuthException):
                            raise
                        raise EeroAuthException(
                            code=err.code,
                            error=err.error,
                            message="Session refresh failed",
                        ) from err
                    finally:
                        self._refresh_in_progress = False
                    response = self.timeout(function)
                    if not response.ok:
                        text = self.decode_json(response)
                        if self._session_rejected(text):
                            self._notify_auth_failed()
                            raise EeroAuthException(
                                code=response.status_code,
                                error=response.reason,
                                message="Session rejected after refresh",
                                payload=redact_secrets(response.text, self.user_token),
                            )
                        raise self._http_error(response)
                else:
                    # The refresh request runs with a refresh already in
                    # progress. Only the outer caller reports auth failure,
                    # so a rejected refresh notifies Home Assistant once.
                    if not self._refresh_in_progress:
                        self._notify_auth_failed()
                    raise EeroAuthException(
                        code=response.status_code,
                        error=response.reason,
                        message="Session rejected",
                        payload=redact_secrets(response.text, self.user_token),
                    )
            else:
                raise self._http_error(response)
        text = self.decode_json(response)
        return text.get("data")

    def timeout(self, function: Callable) -> requests.Response:
        """Timeout."""
        try:
            return function()
        except requests.exceptions.Timeout as exception:
            raise EeroException(
                message="Request timed out",
            ) from exception

    def save_response(self, response: Any, name: str = "response") -> None:
        """Save response."""
        if self.save_location and response:
            if not Path(self.save_location).is_dir():
                _LOGGER.debug("Creating directory: %s", self.save_location)
                Path(self.save_location).mkdir()
            name = name.replace("/", "_").replace(".", "_")
            file_path_name = f"{self.save_location}/{name}.json"
            _LOGGER.debug("Saving response: %s", file_path_name)
            with Path(file_path_name).open(mode="w", encoding="utf-8") as file:
                json.dump(
                    obj=response,
                    fp=file,
                    indent=4,
                    default=lambda o: "not-serializable",
                    sort_keys=True,
                )
            file.close()

    def update(
        self,
        config: dict[str, EeroUpdateConfig] | None = None,
    ) -> EeroAccount:
        """Update."""
        if config is None:
            config = {}
        try:
            account = self.call(method=METHOD_GET, url=URL_ACCOUNT)
            networks = []
            for network in account["networks"]["data"]:
                network_url = network["url"]
                network_id = network_url.replace("/2.2/networks/", "")
                if any(
                    [
                        not config,
                        network_id in config,
                    ]
                ):
                    network_data = self.call(method=METHOD_GET, url=network_url)
                    network_data["thread"] = self.call(
                        method=METHOD_GET,
                        url=network_data["resources"]["thread"],
                    )

                    if all(
                        [
                            any(
                                [
                                    not config,
                                    config.get(
                                        network_id, EeroUpdateConfig()
                                    ).get_backup_access_points,
                                ]
                            ),
                            backup_access_point_ok(
                                capable=network_data["capabilities"][
                                    "backup_access_point"
                                ]["capable"],
                                requirements=network_data["capabilities"][
                                    "backup_access_point"
                                ]["requirements"],
                            ),
                            premium_ok(
                                capable=network_data["capabilities"]["premium"][
                                    "capable"
                                ],
                                status=network_data["premium_status"],
                            ),
                        ]
                    ):
                        backup_access_points = self.call(
                            method=METHOD_GET,
                            url=f"{network_url}/backup_access_points",
                        )
                        network_data["backup_access_points"] = {
                            "count": len(backup_access_points),
                            "data": backup_access_points,
                        }

                    if any(
                        [
                            not config,
                            config.get(network_id, EeroUpdateConfig()).get_devices,
                        ]
                    ):
                        network_data["devices"] = self.get_resource_data(
                            network_data, "devices"
                        )

                    if any(
                        [
                            not config,
                            config.get(network_id, EeroUpdateConfig()).get_profiles,
                        ]
                    ):
                        network_data["profiles"] = self.get_resource_data(
                            network_data, "profiles"
                        )

                    update_data = network_data["updates"]
                    if config.get(network_id, EeroUpdateConfig()).get_release_notes:
                        update_data["release_notes"] = self.get_release_notes(
                            url=update_data["manifest_resource"],
                        )
                    network_data["updates"] = update_data

                    network_id = network_url.replace("/2.2/networks/", "")
                    activity_data = {}
                    for resource, activities in config.get(
                        network_id, EeroUpdateConfig()
                    ).activity.items():
                        resource = RESOURCE_MAP.get(resource, resource)
                        activity_data[resource] = {}
                        for activity in activities:
                            if resource == "profiles":
                                activity_data[resource][activity] = {}
                                for profile_id in config.get(
                                    network_id, EeroUpdateConfig()
                                ).profiles:
                                    activity_data[resource][activity][profile_id] = (
                                        self.update_activity(
                                            activity=activity,
                                            network_url=network_url,
                                            profile_id=profile_id,
                                            resource=resource,
                                            timezone=network_data["timezone"]["value"],
                                        )
                                    )
                            else:
                                activity_data[resource][activity] = (
                                    self.update_activity(
                                        activity=activity,
                                        network_url=network_url,
                                        profile_id=None,
                                        resource=resource,
                                        timezone=network_data["timezone"]["value"],
                                    )
                                )
                    network_data["activity"] = activity_data
                    networks.append(network_data)
            account["networks"]["data"] = networks
            self.save_response(response=redact_data(account, self.user_token), name="update_data")
            self.data = EeroAccount(self, account)
        except EeroAuthException:
            raise
        except EeroException:
            return self.data
        return self.data

    def get_resource_data(
        self,
        network_data: dict,
        resource: str,
    ) -> dict:
        """Get resource data."""
        resource_data = self.call(
            method=METHOD_GET,
            url=network_data["resources"][resource],
        )
        return {
            "count": len(resource_data),
            "data": resource_data,
        }

    def update_activity(
        self,
        activity: str,
        network_url: str,
        profile_id: int,
        resource: str,
        timezone: str,
    ) -> list[dict]:
        """Update activity."""
        activity_url = ACTIVITY_MAP[activity][0].format(network_url)
        if resource != "network":
            activity_url = f"{activity_url}/{resource}"
        if resource == "profiles":
            activity_url = f"{activity_url}/{profile_id}"
        start, end, cadence = self.define_period(
            period=ACTIVITY_MAP[activity][2],
            timezone=timezone,
        )
        json_data = {
            "start": start,
            "end": end,
            "cadence": cadence,
            "timezone": timezone,
        }
        if ACTIVITY_MAP[activity][1]:
            json_data["insight_type"] = ACTIVITY_MAP[activity][1]
        data = self.call(
            method=METHOD_GET,
            url=activity_url,
            json=json_data,
        )
        return data.get("insights", data.get("series", data.get("values")))


class EeroUpdateConfig:
    """A class that describes an Eero update config."""

    def __init__(
        self,
        activity: dict | None = None,
        profiles: list | None = None,
        get_backup_access_points: bool = False,
        get_devices: bool = False,
        get_release_notes: bool = False,
    ) -> None:
        """Initialize."""
        self.activity = activity
        self.profiles = profiles
        self.get_backup_access_points = get_backup_access_points
        self.get_devices = get_devices
        self.get_profiles = bool(profiles)
        self.get_release_notes = get_release_notes
        if self.activity is None:
            self.activity = {}
        if self.profiles is None:
            self.profiles = []
