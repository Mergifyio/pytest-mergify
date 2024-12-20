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
        for span in spans
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
        for span in spans
    )


def test_span_github_actions(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    # Do a partial reconfig, half GHA, half local to have spans
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    result, spans = pytester_with_spans()
    assert (
        spans[0].resource.attributes["vcs.repository.name"]
        == "Mergifyio/pytest-mergify"
    )
