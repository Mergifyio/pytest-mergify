import re
import typing
from unittest import mock

import pytest

from pytest_mergify import utils
from pytest_mergify.resources import github_actions
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
    monkeypatch.setenv("MERGIFY_TEST_JOB_NAME", "f00b4r")

    result, spans = pytester_with_spans()
    assert spans is not None
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


@pytest.mark.parametrize(
    ("head_ref", "ref_name", "expected"),
    [
        pytest.param("feature-branch", "123/merge", "feature-branch", id="PR context"),
        pytest.param(None, "main", "main", id="Without `GITHUB_REF_NAME`"),
        pytest.param("", "main", "main", id="Empty `GITHUB_REF_NAME`"),
        pytest.param(None, None, None, id="Without any variable"),
    ],
)
def test_get_head_ref_name(
    monkeypatch: pytest.MonkeyPatch,
    head_ref: typing.Optional[str],
    ref_name: typing.Optional[str],
    expected: typing.Optional[str],
) -> None:
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    if head_ref:
        monkeypatch.setenv("GITHUB_HEAD_REF", head_ref)
    if ref_name:
        monkeypatch.setenv("GITHUB_REF_NAME", ref_name)

    assert github_actions._get_head_ref_name() == expected


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
