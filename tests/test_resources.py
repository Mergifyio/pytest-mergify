import re
import typing

import pytest


from tests import conftest

from pytest_mergify import utils


def test_span_resources_attributes_ci(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert all(
        span.resource.attributes["cicd.provider.name"] == utils.get_ci_provider()
        for span in spans.values()
    )


def test_span_resources_attributes_pytest(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert all(
        re.match(
            r"\d\.",
            typing.cast(str, span.resource.attributes["test.framework.version"]),
        )
        for span in spans.values()
    )


def test_span_github_actions(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    # Do a partial reconfig, half GHA, half local to have spans
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    result, spans = pytester_with_spans()
    assert all(
        span.resource.attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.repository.url.full"]
        == "https://github.com/Mergifyio/pytest-mergify"
        for span in spans.values()
    )
