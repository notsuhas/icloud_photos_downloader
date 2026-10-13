import logging
from unittest.mock import Mock

import pytest

import icloudpd.base as base
from icloudpd.cli import parse
from icloudpd.server import create_app
from icloudpd.status import Status, StatusExchange
from pyicloud_ipd.exceptions import PyiCloudServiceNotActivatedException
from pyicloud_ipd.services.photos import PhotoLibrary, PhotosService


def test_success_clears_previous_sync_error(monkeypatch: pytest.MonkeyPatch) -> None:
    library = Mock(albums={"All Photos": object()})
    service = Mock()
    service.photos.get_library.return_value = library
    monkeypatch.setattr(base, "authenticator", Mock(return_value=service))
    config, users = parse(
        ["--username", "test@example.com", "--list-albums", "--directory", "/tmp/photos"]
    )
    status = StatusExchange()
    status.set_sync_error("Previous indexing failure")
    result = base.core_single_run(
        logging.getLogger(),
        status,
        config,
        users[0],
        {},
        lambda _p: False,
        lambda _s, _c, _p: False,
        lambda: True,
        lambda name: name,
    )
    assert result == 0
    assert status.get_error() is None


def test_indexing_error_reaches_dashboard_without_immediate_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "Apple iCloud Photo Library has not finished indexing yet (state: RUNNING)"

    class Service:
        response_observer = None

        @property
        def photos(self) -> None:
            raise PyiCloudServiceNotActivatedException(message)

    auth = Mock(return_value=Service())
    monkeypatch.setattr(base, "authenticator", auth)
    config, users = parse(
        [
            "--username",
            "test@example.com",
            "--mfa-provider",
            "webui",
            "--defer-auth",
            "--directory",
            "/tmp/photos",
            "--watch-with-interval",
            "600",
        ]
    )
    status = StatusExchange()
    result = base.core_single_run(
        logging.getLogger(),
        status,
        config,
        users[0],
        {},
        lambda _p: False,
        lambda _s, _c, _p: False,
        lambda: True,
        lambda name: name,
    )
    assert result == 1
    auth.assert_called_once()
    assert status.get_status() == Status.NO_INPUT_NEEDED
    assert status.get_error() == message
    response = create_app(logging.getLogger(), status).test_client().get("/status")
    assert message.encode() in response.data


def test_indexing_error_includes_server_state() -> None:
    session = Mock()
    session.post.return_value.json.return_value = {
        "records": [{"fields": {"state": {"value": "RUNNING"}}}]
    }
    with pytest.raises(PyiCloudServiceNotActivatedException, match="state: RUNNING"):
        PhotoLibrary("https://example.com", {}, session, {"zoneName": "PrimarySync"}, "private")


def test_discovery_preserves_indexing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service = PhotosService.__new__(PhotosService)
    service._service_root = "https://example.com"
    service.params = {}
    service._private_libraries = None
    service._shared_libraries = None
    service._library_errors = {}
    service.session = Mock()
    service.session.post.return_value.json.return_value = {
        "zones": [{"zoneID": {"zoneName": "PrimarySync"}}]
    }
    error = PyiCloudServiceNotActivatedException("Library indexing is RUNNING")
    monkeypatch.setattr("pyicloud_ipd.services.photos.PhotoLibrary", Mock(side_effect=error))
    with pytest.raises(PyiCloudServiceNotActivatedException, match="Library indexing is RUNNING"):
        service.get_library("PrimarySync")


@pytest.mark.parametrize("names", [["PrimarySync", "Other"], ["Other", "PrimarySync"]])
def test_unrelated_indexing_zone_does_not_block_selected_library(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    service = PhotosService.__new__(PhotosService)
    service._service_root = "https://example.com"
    service.params = {}
    service._private_libraries = None
    service._shared_libraries = None
    service._library_errors = {}
    service.session = Mock()
    service.session.post.return_value.json.return_value = {
        "zones": [{"zoneID": {"zoneName": name}} for name in names]
    }
    selected = Mock()

    def make_library(*args: object, **kwargs: object) -> Mock:
        if kwargs["zone_id"] == {"zoneName": "Other"}:
            raise PyiCloudServiceNotActivatedException("Other library indexing is RUNNING")
        return selected

    monkeypatch.setattr("pyicloud_ipd.services.photos.PhotoLibrary", make_library)
    assert service.get_library("PrimarySync") is selected
    with pytest.raises(
        PyiCloudServiceNotActivatedException, match="Other library indexing is RUNNING"
    ):
        service.get_library("Other")
