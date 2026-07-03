import json
import pathlib

import pytest

from pytest_mergify import utils
from pytest_mergify.utils import get_repository_name_from_url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://github.com/owner/repo", "owner/repo"),
        ("https://gitlab.com/owner/repo", "owner/repo"),
        ("https://git.example.com/owner/repo", "owner/repo"),
        ("owner/repo", "owner/repo"),
        ("https://github.com/my-org.name/my-repo.name", "my-org.name/my-repo.name"),
        ("https://git.example.com:8080/owner/repo", "owner/repo"),
        ("https://github.com/owner123/repo456", "owner123/repo456"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("git@gitlab.com:owner/repo.git", "owner/repo"),
        (
            "git@git.example.com:my-org.name/my-repo.name.git",
            "my-org.name/my-repo.name",
        ),
        ("git@bitbucket.org:owner123/repo456.git", "owner123/repo456"),
    ],
)
def test_get_repository_name_from_url_valid(url: str, expected: str) -> None:
    """Test valid URL formats that should extract repository names."""
    result = get_repository_name_from_url(url)
    assert result == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/issues",
        "https://github.com/owner",
        "",
        "not-a-url",
        "https://github.com/owner/repo?tab=readme",
    ],
)
def test_get_repository_name_from_url_invalid(url: str) -> None:
    """Test invalid URL formats that should return None."""
    result = get_repository_name_from_url(url)
    assert result is None


def _write_event_file(tmp_path: pathlib.Path, payload: object) -> str:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload))
    return str(event_file)


def test_is_draft_pull_request_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        _write_event_file(tmp_path, {"pull_request": {"draft": True}}),
    )
    assert utils.is_draft_pull_request() is True


def test_is_draft_pull_request_not_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        _write_event_file(tmp_path, {"pull_request": {"draft": False}}),
    )
    assert utils.is_draft_pull_request() is False


def test_is_draft_pull_request_not_a_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert utils.is_draft_pull_request() is False


def test_is_draft_pull_request_no_event_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert utils.is_draft_pull_request() is False


def test_is_draft_pull_request_missing_event_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    assert utils.is_draft_pull_request() is False


def test_is_draft_pull_request_malformed_event_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text("{ not valid json")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    assert utils.is_draft_pull_request() is False


@pytest.mark.parametrize(
    argnames="payload",
    argvalues=[
        pytest.param([], id="Not an object"),
        pytest.param({}, id="Missing pull request"),
        pytest.param(
            {"pull_request": "not-an-object"}, id="Pull request not an object"
        ),
    ],
)
def test_is_draft_pull_request_unexpected_payload_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, payload: object
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", _write_event_file(tmp_path, payload))
    assert utils.is_draft_pull_request() is False


def test_is_draft_pull_request_target_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        _write_event_file(tmp_path, {"pull_request": {"draft": True}}),
    )
    assert utils.is_draft_pull_request() is True
