import re

import _pytest.pytester
import pytest

from tests import conftest


def _configure_upload(
    monkeypatch: pytest.MonkeyPatch,
    collector: conftest.OTLPCollector,
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_TOKEN", "token")
    monkeypatch.setenv("MERGIFY_API_URL", collector.url)
    # Both of these swap the exporter for one that uploads nothing.
    monkeypatch.delenv("_PYTEST_MERGIFY_TEST", raising=False)
    monkeypatch.delenv("PYTEST_MERGIFY_DEBUG", raising=False)


def test_a_run_uploads_its_spans(
    pytester: _pytest.pytester.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    otlp_collector: conftest.OTLPCollector,
) -> None:
    _configure_upload(monkeypatch, otlp_collector)
    pytester.makepyfile("def test_pass(): pass")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    assert len(otlp_collector.batches) == 1
    assert otlp_collector.span_names == {
        "pytest session start",
        "test_a_run_uploads_its_spans.py::test_pass",
    }


def test_an_uploaded_span_carries_its_attributes(
    pytester: _pytest.pytester.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    otlp_collector: conftest.OTLPCollector,
) -> None:
    # Asserting on the decoded payload rather than on terminal text: a run can
    # print a run id and still have uploaded nothing.
    _configure_upload(monkeypatch, otlp_collector)
    pytester.makepyfile("def test_pass(): pass")

    result = pytester.runpytest_subprocess()

    # Asserted before the payload, so a run that died on the way to uploading
    # reads as the failure it is rather than as a missing key.
    result.assert_outcomes(passed=1)
    (batch,) = otlp_collector.batches
    span = batch.span("test_an_uploaded_span_carries_its_attributes.py::test_pass")

    assert span.attributes["test.case.result.status"] == "passed"
    assert span.attributes["test.scope"] == "case"
    assert (
        batch.resource_attributes["vcs.repository.name"] == "Mergifyio/pytest-mergify"
    )
    # The id the run reported to the user has to be the one it filed the spans
    # under, or the summary sends them looking up somebody else's run.
    printed_run_id = re.search(r"MERGIFY_TEST_RUN_ID=(\w+)", result.stdout.str())
    assert printed_run_id is not None
    assert batch.resource_attributes["test.run.id"] == printed_run_id.group(1)
