import opentelemetry.trace
from opentelemetry.semconv.trace import SpanAttributes


from tests import conftest


def test_span(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert set(spans.keys()) == {
        "pytest session start",
        "test_pass",
    }


def test_session(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    s = spans["pytest session start"]
    assert s.attributes == {"test.scope": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.OK


def test_session_fail(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("def test_fail(): assert False")
    s = spans["pytest session start"]
    assert s.attributes == {"test.scope": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.ERROR


def test_test(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    session_span = spans["pytest session start"]

    assert spans["test_pass"].attributes == {
        "test.scope": "case",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "test.case.result.status": "passed",
    }
    assert spans["test_pass"].status.status_code == opentelemetry.trace.StatusCode.OK
    assert session_span.context is not None
    assert spans["test_pass"].parent is not None
    assert spans["test_pass"].parent.span_id == session_span.context.span_id


def test_test_failure(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("def test_error(): assert False, 'foobar'")
    session_span = spans["pytest session start"]

    assert spans["test_error"].attributes == {
        "test.case.result.status": "failed",
        "test.scope": "case",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        SpanAttributes.EXCEPTION_TYPE: "<class 'AssertionError'>",
        SpanAttributes.EXCEPTION_MESSAGE: "foobar\nassert False",
        SpanAttributes.EXCEPTION_STACKTRACE: """>   def test_error(): assert False, 'foobar'
E   AssertionError: foobar
E   assert False

test_test_failure.py:1: AssertionError""",
    }
    assert (
        spans["test_error"].status.status_code == opentelemetry.trace.StatusCode.ERROR
    )
    assert (
        spans["test_error"].status.description
        == "<class 'AssertionError'>: foobar\nassert False"
    )
    assert session_span.context is not None
    assert spans["test_error"].parent is not None
    assert spans["test_error"].parent.span_id == session_span.context.span_id


def test_test_skipped(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("""
import pytest
def test_skipped():
    pytest.skip('not needed')
""")
    session_span = spans["pytest session start"]

    assert spans["test_skipped"].attributes == {
        "test.case.result.status": "skipped",
        "test.scope": "case",
        "code.function": "test_skipped",
        "code.lineno": 1,
        "code.filepath": "test_test_skipped.py",
    }
    assert spans["test_skipped"].status.status_code == opentelemetry.trace.StatusCode.OK
    assert session_span.context is not None
    assert spans["test_skipped"].parent is not None
    assert spans["test_skipped"].parent.span_id == session_span.context.span_id


def test_span_resources_test_run_id(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert all(
        isinstance(span.resource.attributes["test.run.id"], str)
        and len(span.resource.attributes["test.run.id"]) == 16
        and int(span.resource.attributes["test.run.id"], 16) > 0
        for span in spans.values()
    )
