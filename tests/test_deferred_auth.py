import argparse
import json
import logging
import time
from functools import partial
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from icloudpd.authentication import authenticator
from icloudpd.base import core_single_run, get_password_from_webui, update_password_status_in_webui
from icloudpd.cli import parse
from icloudpd.mfa_provider import MFAProvider
from icloudpd.password_provider import PasswordProvider
from icloudpd.server import create_app
from icloudpd.status import Status, StatusExchange
from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.exceptions import PyiCloudAPIResponseException, PyiCloudFailedLoginException


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Authentication did not reach the expected state"
        time.sleep(0.01)


@pytest.fixture
def auth_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    status = StatusExchange()
    status.set_current_user("test@example.com")
    notifier = Mock()
    password = Mock(return_value="test-password")
    push = Mock(return_value=True)
    srp = Mock()
    account = {
        "dsInfo": {"dsid": "test", "hsaVersion": 2, "hasICloudQualifyingDevice": True},
        "hsaChallengeRequired": True,
        "hsaTrustedBrowser": False,
        "webservices": {},
    }

    def token_login(service: PyiCloudService) -> None:
        service.data = dict(account)

    def validate(service: PyiCloudService, code: str) -> bool:
        if code != "123456":
            return False
        service.data["hsaTrustedBrowser"] = True
        service.data["hsaChallengeRequired"] = False
        return True

    monkeypatch.setattr(PyiCloudService, "_authenticate_srp", srp)
    monkeypatch.setattr(PyiCloudService, "_authenticate_with_token", token_login)
    monkeypatch.setattr(PyiCloudService, "trigger_push_notification", push)
    monkeypatch.setattr(PyiCloudService, "validate_2fa_code", validate)
    results: list[PyiCloudService] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            results.append(
                authenticator(
                    logging.getLogger(__name__),
                    "com",
                    {"test": (password, lambda _u, _p: None)},
                    MFAProvider.WEBUI,
                    status,
                    "test@example.com",
                    notifier,
                    cookie_directory=str(tmp_path),
                    defer_auth=True,
                )
            )
        except Exception as error:
            errors.append(error)

    return {
        "status": status,
        "notifier": notifier,
        "password": password,
        "push": push,
        "srp": srp,
        "results": results,
        "errors": errors,
        "run": run,
        "cookies": tmp_path,
        "account": account,
        "client": create_app(logging.getLogger(__name__), status).test_client(),
    }


@pytest.mark.parametrize("session", ["missing", "expired", "untrusted"])
def test_notify_wait_request_retry_and_resume(
    auth_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    flow = auth_flow
    if session != "missing":
        (flow["cookies"] / "testexamplecom.session").write_text(
            json.dumps({"session_token": "saved-token"})
        )
        validator = Mock(return_value=flow["account"])
        if session == "expired":
            validator.side_effect = PyiCloudAPIResponseException("Expired", "421")
        monkeypatch.setattr(PyiCloudService, "_validate_token", validator)
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    status = flow["status"]
    client = flow["client"]
    wait_until(lambda: status.get_status() == Status.LOGIN_REQUIRED)
    flow["notifier"].assert_called_once()
    flow["password"].assert_not_called()
    flow["srp"].assert_not_called()
    flow["push"].assert_not_called()
    assert b"Start login" in client.get("/status").data
    assert client.post("/code", data={"code": "123456"}).status_code == 400
    assert client.post("/login").status_code == 200
    assert client.post("/login").status_code == 409
    wait_until(lambda: status.get_status() == Status.NEED_MFA)
    flow["srp"].assert_called_once()
    flow["push"].assert_called_once()
    assert b"Request a new code" in client.get("/status").data
    assert client.post("/code", data={"code": ""}).status_code == 400
    assert status.get_status() == Status.NEED_MFA
    assert client.post("/code", data={"code": "000000"}).status_code == 200
    wait_until(lambda: status.get_error() is not None)
    assert b"Failed to verify" in client.get("/status").data
    assert client.post("/login").status_code == 200
    wait_until(lambda: flow["push"].call_count == 2)
    assert flow["srp"].call_count == 2
    flow["notifier"].assert_called_once()
    assert client.post("/code", data={"code": "123456"}).status_code == 200
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not flow["errors"]
    assert len(flow["results"]) == 1
    assert status.get_status() == Status.NO_INPUT_NEEDED
    status.require_login(flow["notifier"])
    assert flow["notifier"].call_count == 2


def test_valid_session_needs_no_login(
    auth_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = auth_flow
    (flow["cookies"] / "testexamplecom.session").write_text(
        json.dumps({"session_token": "saved-token"})
    )
    account = dict(flow["account"], hsaTrustedBrowser=True, hsaChallengeRequired=False)
    monkeypatch.setattr(PyiCloudService, "_validate_token", Mock(return_value=account))
    flow["run"]()
    assert not flow["errors"]
    assert len(flow["results"]) == 1
    flow["notifier"].assert_not_called()
    flow["password"].assert_not_called()
    flow["push"].assert_not_called()


def test_login_error_waits_for_another_click(auth_flow: dict[str, Any]) -> None:
    flow = auth_flow
    flow["srp"].side_effect = PyiCloudAPIResponseException("Unavailable", "503")
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    wait_until(lambda: flow["status"].get_status() == Status.LOGIN_REQUIRED)
    assert flow["status"].request_login()
    worker.join(timeout=5)
    assert len(flow["errors"]) == 1
    assert flow["status"].set_error(str(flow["errors"][0]))
    flow["errors"].clear()
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    wait_until(lambda: flow["status"].get_status() == Status.LOGIN_REQUIRED)
    flow["notifier"].assert_called_once()
    flow["srp"].assert_called_once()
    flow["srp"].side_effect = None
    assert flow["status"].request_login()
    wait_until(lambda: flow["status"].get_status() == Status.NEED_MFA)
    assert flow["status"].set_payload("123456")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not flow["errors"]


def test_push_failure_can_be_retried(auth_flow: dict[str, Any]) -> None:
    flow = auth_flow
    flow["push"].side_effect = [False, True]
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    wait_until(lambda: flow["status"].get_status() == Status.LOGIN_REQUIRED)
    assert flow["status"].request_login()
    wait_until(lambda: flow["status"].get_status() == Status.NEED_MFA)
    assert "Could not request a code" in flow["status"].get_error()
    assert flow["status"].request_login()
    wait_until(lambda: flow["push"].call_count == 2)
    assert flow["status"].set_payload("123456")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not flow["errors"]


def test_defer_auth_requires_webui() -> None:
    with pytest.raises(argparse.ArgumentError, match="requires --mfa-provider webui"):
        parse(["--username", "test@example.com", "--defer-auth"])
    config, _ = parse(["--username", "test@example.com", "--mfa-provider", "webui", "--defer-auth"])
    assert config.defer_auth


def test_later_expiry_requires_another_click(auth_flow: dict[str, Any]) -> None:
    flow = auth_flow
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    wait_until(lambda: flow["status"].get_status() == Status.LOGIN_REQUIRED)
    assert flow["status"].request_login()
    wait_until(lambda: flow["status"].get_status() == Status.NEED_MFA)
    assert flow["status"].set_payload("123456")
    worker.join(timeout=5)
    assert not worker.is_alive()
    service = flow["results"][0]
    worker = Thread(target=lambda: service.authenticate(force_refresh=True), daemon=True)
    worker.start()
    wait_until(lambda: flow["status"].get_status() == Status.LOGIN_REQUIRED)
    assert flow["notifier"].call_count == 2
    flow["srp"].assert_called_once()
    flow["push"].assert_called_once()
    assert flow["status"].request_login()
    wait_until(lambda: flow["status"].get_status() == Status.NEED_MFA)
    assert flow["status"].set_payload("123456")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert flow["push"].call_count == 2
    assert flow["status"].get_status() == Status.NO_INPUT_NEEDED


@pytest.mark.parametrize("saved_password", [None, "rejected-password"])
def test_password_fallback_and_core_retry(
    auth_flow: dict[str, Any], saved_password: str | None
) -> None:
    flow = auth_flow
    status = flow["status"]
    logger = logging.getLogger(__name__)
    global_config, users = parse(
        [
            "--username",
            "test@example.com",
            "--auth-only",
            "--cookie-directory",
            str(flow["cookies"]),
            "--mfa-provider",
            "webui",
            "--defer-auth",
            "--password-provider",
            "keyring",
            "--password-provider",
            "webui",
        ]
    )

    def srp(password: str) -> None:
        if password == "rejected-password":
            raise PyiCloudFailedLoginException("Invalid password")

    flow["srp"].side_effect = srp
    writer = Mock()
    providers: dict[
        PasswordProvider,
        tuple[Callable[[str], str | None], Callable[[str, str], None]],
    ] = {
        PasswordProvider.KEYRING: (Mock(return_value=saved_password), writer),
        PasswordProvider.WEBUI: (
            partial(get_password_from_webui, logger, status),
            partial(update_password_status_in_webui, status),
        ),
    }
    results: list[int] = []
    worker = Thread(
        target=lambda: results.append(
            core_single_run(
                logger,
                status,
                global_config,
                users[0],
                providers,
                lambda _p: False,
                lambda _s, _c, _p: False,
                flow["notifier"],
                lambda name: name,
            )
        ),
        daemon=True,
    )
    worker.start()
    wait_until(lambda: status.get_status() == Status.LOGIN_REQUIRED)
    assert flow["client"].post("/login").status_code == 200
    if saved_password:
        wait_until(lambda: status.get_status() == Status.LOGIN_REQUIRED)
        assert flow["client"].post("/login").status_code == 200
    wait_until(lambda: status.get_status() == Status.NEED_PASSWORD)
    assert flow["client"].post("/password", data={"password": "replacement"}).status_code == 200
    wait_until(lambda: status.get_status() == Status.NEED_MFA)
    assert flow["client"].post("/code", data={"code": "123456"}).status_code == 200
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results == [0]
    writer.assert_called_once_with("test@example.com", "replacement")
    flow["notifier"].assert_called_once()


def test_failed_notification_retries_without_login(
    auth_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import icloudpd.authentication as authentication

    class Clock:
        now = 0

        def monotonic(self) -> int:
            self.now += 61
            return self.now

        def sleep(self, _seconds: float) -> None:
            time.sleep(0.01)

    monkeypatch.setattr(authentication, "time", Clock())
    flow = auth_flow
    flow["notifier"].side_effect = [False, True]
    worker = Thread(target=flow["run"], daemon=True)
    worker.start()
    wait_until(lambda: flow["notifier"].call_count == 2)
    flow["srp"].assert_not_called()
    flow["push"].assert_not_called()
    assert flow["status"].request_login()
    wait_until(lambda: flow["status"].get_status() == Status.NEED_MFA)
    assert flow["status"].set_payload("123456")
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not flow["errors"]


def test_dashboard_redacts_passwords(auth_flow: dict[str, Any]) -> None:
    global_config, users = parse(
        [
            "--username",
            "test@example.com",
            "--password",
            "APPLE_SECRET",
            "--smtp-password",
            "SMTP_SECRET",
        ]
    )
    auth_flow["status"].set_global_config(global_config)
    auth_flow["status"].set_user_configs(users)
    response = auth_flow["client"].get("/status")
    assert response.status_code == 200
    assert b"APPLE_SECRET" not in response.data
    assert b"SMTP_SECRET" not in response.data
