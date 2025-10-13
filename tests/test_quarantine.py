import pytest
import requests
import responses

from pytest_mergify import utils
from pytest_mergify.quarantine import Quarantine


@responses.activate
def test_quarantine_handles_requests_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("MERGIFY_API_URL", "https://example.com")

    responses.add(
        responses.GET,
        "https://example.com/v1/ci/owner/repositories/repo/quarantines",
        body=requests.ReadTimeout("boom"),
    )

    q = Quarantine(
        api_ctxt=utils.APIContext(
            token="tok",
            url="https://example.com",
            full_repository_name="owner/repo",
            branch_name="main",
        )
    )

    assert q.init_error_msg is not None
    assert "Failed to connect to Mergify's API" in q.init_error_msg
    # Should not have populated quarantined tests
    assert q.quarantined_tests == []
