import typing

import pytest
import responses
import requests

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
        api_url="https://example.com",
        token="tok",
        repo_name="owner/repo",
        branch_name="main",
    )

    assert q.init_error_msg is not None
    assert "Failed to connect to Mergify's API" in q.init_error_msg
    # Should not have populated quarantined tests
    assert q.quarantined_tests == []


@responses.activate
def test_quarantine_walks_paginated_pages() -> None:
    base_url = "https://example.com/v1/ci/owner/repositories/repo/quarantines"
    page2_url = f"{base_url}?cursor=PAGE2&per_page=100"
    page3_url = f"{base_url}?cursor=PAGE3&per_page=100"

    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_a"}, {"test_name": "test_b"}]},
        headers={"Link": f'<{page2_url}>; rel="next"'},
        match=[
            responses.matchers.query_param_matcher(
                {"branch": "main", "per_page": "100"}
            )
        ],
    )
    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_c"}]},
        headers={"Link": f'<{page3_url}>; rel="next"'},
        match=[
            responses.matchers.query_param_matcher(
                {"cursor": "PAGE2", "per_page": "100"}
            )
        ],
    )
    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_d"}]},
        match=[
            responses.matchers.query_param_matcher(
                {"cursor": "PAGE3", "per_page": "100"}
            )
        ],
    )

    q = Quarantine(
        api_url="https://example.com",
        token="tok",
        repo_name="owner/repo",
        branch_name="main",
    )

    assert q.init_error_msg is None
    assert q.quarantined_tests == ["test_a", "test_b", "test_c", "test_d"]


@responses.activate
def test_quarantine_resets_on_mid_pagination_error() -> None:
    base_url = "https://example.com/v1/ci/owner/repositories/repo/quarantines"
    page2_url = f"{base_url}?cursor=PAGE2&per_page=100"

    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_a"}]},
        headers={"Link": f'<{page2_url}>; rel="next"'},
        match=[
            responses.matchers.query_param_matcher(
                {"branch": "main", "per_page": "100"}
            )
        ],
    )
    responses.add(
        responses.GET,
        base_url,
        status=500,
        match=[
            responses.matchers.query_param_matcher(
                {"cursor": "PAGE2", "per_page": "100"}
            )
        ],
    )

    q = Quarantine(
        api_url="https://example.com",
        token="tok",
        repo_name="owner/repo",
        branch_name="main",
    )

    assert q.init_error_msg is not None
    assert "Error when querying Mergify's API" in q.init_error_msg
    # No partial population: a failure on page 2 must not leak page 1's data.
    assert q.quarantined_tests == []


@pytest.mark.parametrize(
    argnames="response_kwargs",
    argvalues=[
        pytest.param(
            {"body": "<html>502 Bad Gateway</html>", "content_type": "text/html"},
            id="an-error-page-served-as-200",
        ),
        pytest.param({"json": {"items": []}}, id="a-renamed-collection"),
        pytest.param(
            {"json": {"quarantined_tests": [{"name": "test_a"}]}},
            id="a-renamed-entry-field",
        ),
        pytest.param({"json": []}, id="a-payload-that-is-not-an-object"),
    ],
)
@responses.activate
def test_quarantine_handles_a_malformed_payload(
    response_kwargs: typing.Dict[str, typing.Any],
) -> None:
    # A proxy, WAF or CDN can answer 200 with its own error page, and the API's
    # schema can move under us. Neither is the user's problem to debug from a
    # session that refused to start.
    responses.add(
        responses.GET,
        "https://example.com/v1/ci/owner/repositories/repo/quarantines",
        status=200,
        **response_kwargs,
    )

    q = Quarantine(
        api_url="https://example.com",
        token="tok",
        repo_name="owner/repo",
        branch_name="main",
    )

    assert q.init_error_msg is not None
    # Names the payload path: the transport and status handlers beside it end
    # their messages the same way, so a weaker match passes on either of them.
    assert "Unexpected response" in q.init_error_msg
    assert q.quarantined_tests == []


@responses.activate
def test_quarantine_aborts_on_pagination_cycle() -> None:
    base_url = "https://example.com/v1/ci/owner/repositories/repo/quarantines"
    # `next` link cycles back to a previously fetched URL.
    cycling_url = f"{base_url}?cursor=LOOP&per_page=100"

    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_a"}]},
        headers={"Link": f'<{cycling_url}>; rel="next"'},
        match=[
            responses.matchers.query_param_matcher(
                {"branch": "main", "per_page": "100"}
            )
        ],
    )
    responses.add(
        responses.GET,
        base_url,
        json={"quarantined_tests": [{"test_name": "test_b"}]},
        # Page 2 advertises itself as the next link, forming a cycle.
        headers={"Link": f'<{cycling_url}>; rel="next"'},
        match=[
            responses.matchers.query_param_matcher(
                {"cursor": "LOOP", "per_page": "100"}
            )
        ],
    )

    q = Quarantine(
        api_url="https://example.com",
        token="tok",
        repo_name="owner/repo",
        branch_name="main",
    )

    assert q.init_error_msg is not None
    assert "cyclic" in q.init_error_msg
    # Cycle detected before assigning to self; no partial data leaks.
    assert q.quarantined_tests == []
