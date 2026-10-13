from enum import Enum
from threading import Lock
from typing import Callable, Sequence

from icloudpd.config import GlobalConfig, UserConfig
from icloudpd.progress import Progress


class Status(Enum):
    NO_INPUT_NEEDED = "no_input_needed"
    LOGIN_REQUIRED = "login_required"
    LOGIN_REQUESTED = "login_requested"
    REQUESTING_MFA = "requesting_mfa"
    NEED_MFA = "need_mfa"
    SUPPLIED_MFA = "supplied_mfa"
    CHECKING_MFA = "checking_mfa"
    NEED_PASSWORD = "need_password"
    SUPPLIED_PASSWORD = "supplied_password"
    CHECKING_PASSWORD = "checking_password"

    def __str__(self) -> str:
        return self.name


class StatusExchange:
    def __init__(self) -> None:
        self.lock = Lock()
        self._status = Status.NO_INPUT_NEEDED
        self._payload: str | None = None
        self._error: str | None = None
        self._global_config: GlobalConfig | None = None
        self._user_configs: Sequence[UserConfig] = []
        self._current_user: str | None = None
        self._progress = Progress()
        self._login_notified = False

    def require_login(self, notificator: Callable[[], bool | None]) -> None:
        with self.lock:
            if self._status != Status.LOGIN_REQUESTED:
                self._status = Status.LOGIN_REQUIRED
            notify = not self._login_notified
        if notify:
            delivered = notificator() is not False
            with self.lock:
                self._login_notified = delivered
                if self._status == Status.LOGIN_REQUIRED:
                    self._error = (
                        None
                        if delivered
                        else "Could not send the login notification. Retrying in one minute."
                    )

    def request_login(self) -> bool:
        with self.lock:
            if self._status not in [Status.LOGIN_REQUIRED, Status.NEED_MFA]:
                return False
            self._status = Status.LOGIN_REQUESTED
            self._error = None
            self._payload = None
            return True

    def login_completed(self) -> None:
        with self.lock:
            self._login_notified = False
            self._error = None
            self._payload = None

    def mfa_requested(self, error: str | None = None) -> None:
        with self.lock:
            self._status = Status.NEED_MFA
            self._error = error

    def get_status(self) -> Status:
        with self.lock:
            return self._status

    def replace_status(self, expected_status: Status, new_status: Status) -> bool:
        with self.lock:
            if self._status == expected_status:
                self._status = new_status
                return True
            else:
                return False

    def set_payload(self, payload: str) -> bool:
        with self.lock:
            if self._status != Status.NEED_MFA and self._status != Status.NEED_PASSWORD:
                return False

            self._payload = payload
            self._status = (
                Status.SUPPLIED_MFA if self._status == Status.NEED_MFA else Status.SUPPLIED_PASSWORD
            )
            self._error = None
            return True

    def get_payload(self) -> str | None:
        with self.lock:
            if self._status not in [
                Status.SUPPLIED_MFA,
                Status.CHECKING_MFA,
                Status.SUPPLIED_PASSWORD,
                Status.CHECKING_PASSWORD,
            ]:
                return None

            return self._payload

    def set_error(self, error: str) -> bool:
        with self.lock:
            if self._status in [
                Status.LOGIN_REQUIRED,
                Status.LOGIN_REQUESTED,
                Status.REQUESTING_MFA,
            ]:
                self._error = error
                self._status = Status.LOGIN_REQUIRED
                return True
            if self._status == Status.NO_INPUT_NEEDED and self._login_notified:
                self._error = error
                self._status = Status.LOGIN_REQUIRED
                return True
            if self._status != Status.CHECKING_MFA and self._status != Status.CHECKING_PASSWORD:
                return False

            self._error = error
            self._status = (
                Status.NO_INPUT_NEEDED
                if self._status == Status.CHECKING_PASSWORD
                else Status.NEED_MFA
            )
            return True

    def get_error(self) -> str | None:
        with self.lock:
            if self._status not in [
                Status.NO_INPUT_NEEDED,
                Status.LOGIN_REQUIRED,
                Status.NEED_PASSWORD,
                Status.NEED_MFA,
            ]:
                return None

            return self._error

    def set_sync_error(self, error: str | None) -> None:
        with self.lock:
            self._error = error

    def get_progress(self) -> Progress:
        with self.lock:
            return self._progress

    def set_global_config(self, global_config: GlobalConfig) -> None:
        with self.lock:
            self._global_config = global_config

    def get_global_config(self) -> GlobalConfig | None:
        with self.lock:
            return self._global_config

    def set_user_configs(self, user_configs: Sequence[UserConfig]) -> None:
        with self.lock:
            self._user_configs = user_configs

    def get_user_configs(self) -> Sequence[UserConfig]:
        with self.lock:
            return self._user_configs

    def set_current_user(self, username: str) -> None:
        with self.lock:
            self._current_user = username

    def get_current_user(self) -> str | None:
        with self.lock:
            return self._current_user

    def clear_current_user(self) -> None:
        with self.lock:
            self._current_user = None
