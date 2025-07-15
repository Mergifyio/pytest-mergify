import opentelemetry.trace
from opentelemetry.semconv.trace import SpanAttributes

import pytest

from tests import conftest


def test_span(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    assert set(spans.keys()) == {
        "pytest session start",
        "test_pass",
    }


def test_session(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    s = spans["pytest session start"]
    assert s.attributes == {"test.scope": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.OK


def test_session_fail(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("def test_fail(): assert False")
    assert spans is not None
    s = spans["pytest session start"]
    assert s.attributes == {"test.scope": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.ERROR


def test_test(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    session_span = spans["pytest session start"]

    assert spans["test_pass"].attributes == {
        "test.scope": "case",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "code.namespace": "",
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
    assert spans is not None
    session_span = spans["pytest session start"]

    assert spans["test_error"].attributes == {
        "test.case.result.status": "failed",
        "test.scope": "case",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        "code.namespace": "",
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
    assert spans is not None
    session_span = spans["pytest session start"]

    assert spans["test_skipped"].attributes == {
        "test.case.result.status": "skipped",
        "test.scope": "case",
        "code.function": "test_skipped",
        "code.lineno": 1,
        "code.filepath": "test_test_skipped.py",
        "code.namespace": "",
    }
    assert spans["test_skipped"].status.status_code == opentelemetry.trace.StatusCode.OK
    assert session_span.context is not None
    assert spans["test_skipped"].parent is not None
    assert spans["test_skipped"].parent.span_id == session_span.context.span_id


@pytest.mark.parametrize(
    "mark",
    [
        "skip",
        "skipif(True, reason='not needed')",
        "skipif(1 + 1, reason='with eval')",
        "skipif('1 + 1', reason='as str')",
        "skipif('sys.version_info.major > 1', reason='not needed')",
    ],
)
def test_mark_skipped(
    mark: str,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans(f"""
import pytest
@pytest.mark.{mark}
def test_skipped():
    assert False
""")
    assert spans is not None
    session_span = spans["pytest session start"]

    assert spans["test_skipped"].attributes == {
        "test.case.result.status": "skipped",
        "test.scope": "case",
        "code.function": "test_skipped",
        "code.lineno": 1,
        "code.filepath": "test_mark_skipped.py",
        "code.namespace": "",
    }
    assert (
        spans["test_skipped"].status.status_code == opentelemetry.trace.StatusCode.UNSET
    )
    assert session_span.context is not None
    assert spans["test_skipped"].parent is not None
    assert spans["test_skipped"].parent.span_id == session_span.context.span_id


def test_mark_not_skipped(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("""
import pytest
@pytest.mark.skipif(False, reason='not skipped')
def test_not_skipped():
    assert True
""")
    assert spans is not None
    session_span = spans["pytest session start"]

    assert spans["test_not_skipped"].attributes == {
        "test.case.result.status": "passed",
        "test.scope": "case",
        "code.function": "test_not_skipped",
        "code.lineno": 1,
        "code.filepath": "test_mark_not_skipped.py",
        "code.namespace": "",
    }
    assert (
        spans["test_not_skipped"].status.status_code
        == opentelemetry.trace.StatusCode.OK
    )
    assert session_span.context is not None
    assert spans["test_not_skipped"].parent is not None
    assert spans["test_not_skipped"].parent.span_id == session_span.context.span_id


def test_span_attributes_namespace(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("""
class TestClassBasic:
    def test_namespace(self):
        assert True

def test_namespace():
    assert True

""")
    assert spans is not None

    assert "test_namespace" in spans
    assert "TestClassBasic.test_namespace" in spans


def test_span_resources_test_run_id(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert spans is not None
    assert all(
        isinstance(span.resource.attributes["test.run.id"], str)
        and len(span.resource.attributes["test.run.id"]) == 16
        and int(span.resource.attributes["test.run.id"], 16) > 0
        for span in spans.values()
    )
