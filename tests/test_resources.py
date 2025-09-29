import re
import typing
from unittest import mock

import pytest

from pytest_mergify import utils
from tests import conftest


def test_span_resources_attributes_ci(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        span.resource.attributes["cicd.provider.name"] == utils.get_ci_provider()
        for span in spans.values()
    )


def test_span_resources_attributes_pytest(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        re.match(
            r"\d\.",
            typing.cast(str, span.resource.attributes["test.framework.version"]),
        )
        for span in spans.values()
    )


def test_span_resources_attributes_mergify(
    pytester_with_spans: conftest.PytesterWithSpanT,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERGIFY_TEST_FLAKY_DETECTION", "yes")
    monkeypatch.setenv("MERGIFY_TEST_JOB_NAME", "f00b4r")

    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        span.resource.attributes["mergify.test.flaky_detection_enabled"] is True
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["mergify.test.job.name"] == "f00b4r"
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
    monkeypatch.setenv("GITHUB_RUN_ID", "3213121312")
    monkeypatch.setenv("RUNNER_NAME", "self-hosted")
    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        span.resource.attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.repository.url.full"]
        == "https://github.com/Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["cicd.pipeline.run.id"] == 3213121312
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["cicd.pipeline.runner.name"] == "self-hosted"
        for span in spans.values()
    )


@mock.patch("pytest_mergify.utils.git", return_value=None)
def test_span_jenkins(
    git: mock.Mock,
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.com")
    monkeypatch.setenv(
        "BUILD_URL", "https://jenkins.example.com/Mergifyio/pytest-mergify"
    )
    monkeypatch.setenv("BUILD_ID", "jenkins-job-name#5")
    monkeypatch.setenv("JOB_NAME", "jenkins-job-name")
    monkeypatch.setenv("GIT_URL", "https://github.com/Mergifyio/pytest-mergify")
    monkeypatch.setenv("GIT_BRANCH", "origin/main")
    monkeypatch.setenv("GIT_COMMIT", "1860cf377dd5610e256ff52e47cf38816cc04549")
    monkeypatch.setenv("NODE_NAME", "self-hosted")
    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        span.resource.attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.repository.url.full"]
        == "https://github.com/Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["cicd.pipeline.run.id"] == "jenkins-job-name#5"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["cicd.pipeline.runner.name"] == "self-hosted"
        for span in spans.values()
    )


@mock.patch("pytest_mergify.utils.git", return_value=None)
def test_span_git(
    git: mock.Mock,
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    git.side_effect = [
        "main",
        "azerty",
        "https://github.com/Mergifyio/pytest-mergify",
        "https://github.com/Mergifyio/pytest-mergify",
        "main",
        "azerty",
        "https://github.com/Mergifyio/pytest-mergify",
        "https://github.com/Mergifyio/pytest-mergify",
        "main",
        "azerty",
        "https://github.com/Mergifyio/pytest-mergify",
        "https://github.com/Mergifyio/pytest-mergify",
    ]

    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        span.resource.attributes["vcs.repository.url.full"]
        == "https://github.com/Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.ref.head.name"] == "main"
        for span in spans.values()
    )
    assert all(
        span.resource.attributes["vcs.ref.head.revision"] == "azerty"
        for span in spans.values()
    )
