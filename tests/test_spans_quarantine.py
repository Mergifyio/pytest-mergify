from tests import conftest
import opentelemetry.trace


def test_spans_quarantine(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans(
        """
import pytest

def test_my_not_flaky_success_test():
    assert True

def test_my_not_flaky_failure_test():
    assert False

def test_my_very_flaky_failure_test():
    assert False

def test_my_very_flaky_success_test():
    assert True
""",
        setenv={
            "MERGIFY_TOKEN": "foobar",
            "GITHUB_ACTIONS": "true",
            "GITHUB_BASE_REF": "main",
            "GITHUB_REPOSITORY": "foo/bar",
        },
        quarantined_tests=[
            "test_spans_quarantine.py::test_my_very_flaky_failure_test",
            "test_spans_quarantine.py::test_my_very_flaky_success_test",
        ],
    )
    assert spans is not None

    assert "test_spans_quarantine.py::test_my_not_flaky_success_test" in spans
    assert (
        spans[
            "test_spans_quarantine.py::test_my_not_flaky_success_test"
        ].status.status_code
        == opentelemetry.trace.StatusCode.OK
    )

    assert "test_spans_quarantine.py::test_my_not_flaky_failure_test" in spans

    assert (
        spans[
            "test_spans_quarantine.py::test_my_not_flaky_failure_test"
        ].status.status_code
        == opentelemetry.trace.StatusCode.ERROR
    )

    assert "test_spans_quarantine.py::test_my_very_flaky_failure_test" in spans
    assert "test_spans_quarantine.py::test_my_very_flaky_success_test" in spans
    assert (
        spans[
            "test_spans_quarantine.py::test_my_very_flaky_failure_test"
        ].status.status_code
        == opentelemetry.trace.StatusCode.OK
    )

    assert (
        spans[
            "test_spans_quarantine.py::test_my_very_flaky_success_test"
        ].status.status_code
        == opentelemetry.trace.StatusCode.OK
    )
