import typing

import opentelemetry.trace
from opentelemetry.semconv.trace import SpanAttributes

import anys

from tests import conftest


def test_span(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    assert set(spans.keys()) == {
        "pytest session start",
        "test_span.py::test_pass",
        "test_span.py::test_pass::setup",
        "test_span.py::test_pass::call",
        "test_span.py::test_pass::teardown",
    }


def test_session(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    s = spans["pytest session start"]
    assert s.attributes == {"test.type": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.OK


def test_session_fail(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("def test_fail(): assert False")
    s = spans["pytest session start"]
    assert s.attributes == {"test.type": "session"}
    assert s.status.status_code == opentelemetry.trace.StatusCode.ERROR


def test_test(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans()
    session_span = spans["pytest session start"]

    expected_spans = (
        "test_test.py::test_pass",
        "test_test.py::test_pass::setup",
        "test_test.py::test_pass::call",
        "test_test.py::test_pass::teardown",
    )
    for name in expected_spans:
        assert name in spans

    assert spans["test_test.py::test_pass"].attributes == {
        "test.type": "test",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "test.case.name": "test_test.py::test_pass",
    }
    assert (
        spans["test_test.py::test_pass"].status.status_code
        == opentelemetry.trace.StatusCode.OK
    )
    assert session_span.context is not None
    assert spans["test_test.py::test_pass"].parent is not None
    assert (
        spans["test_test.py::test_pass"].parent.span_id == session_span.context.span_id
    )

    assert spans["test_test.py::test_pass::setup"].attributes == {
        "test.type": "test.setup",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "test.case.name": "test_test.py::test_pass",
    }
    assert (
        spans["test_test.py::test_pass::setup"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["test_test.py::test_pass::call"].attributes == {
        "test.type": "test.call",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "test.case.name": "test_test.py::test_pass",
    }
    assert (
        spans["test_test.py::test_pass::call"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["test_test.py::test_pass::teardown"].attributes == {
        "test.type": "test.teardown",
        "code.function": "test_pass",
        "code.lineno": 0,
        "code.filepath": "test_test.py",
        "test.case.name": "test_test.py::test_pass",
    }
    assert (
        spans["test_test.py::test_pass::teardown"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )


def test_test_failure(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("def test_error(): assert False, 'foobar'")
    session_span = spans["pytest session start"]

    expected_spans = (
        "test_test_failure.py::test_error",
        "test_test_failure.py::test_error::setup",
        "test_test_failure.py::test_error::call",
    )
    for name in expected_spans:
        assert name in spans

    assert spans["test_test_failure.py::test_error"].attributes == {
        "test.type": "test",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        "test.case.name": "test_test_failure.py::test_error",
        SpanAttributes.EXCEPTION_TYPE: "<class 'AssertionError'>",
        SpanAttributes.EXCEPTION_MESSAGE: "foobar\nassert False",
        SpanAttributes.EXCEPTION_STACKTRACE: """>   def test_error(): assert False, 'foobar'
E   AssertionError: foobar
E   assert False

test_test_failure.py:1: AssertionError""",
    }
    assert (
        spans["test_test_failure.py::test_error"].status.status_code
        == opentelemetry.trace.StatusCode.ERROR
    )
    assert (
        spans["test_test_failure.py::test_error"].status.description
        == "<class 'AssertionError'>: foobar\nassert False"
    )
    assert session_span.context is not None
    assert spans["test_test_failure.py::test_error"].parent is not None
    assert (
        spans["test_test_failure.py::test_error"].parent.span_id
        == session_span.context.span_id
    )

    assert spans["test_test_failure.py::test_error::setup"].attributes == {
        "test.type": "test.setup",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        "test.case.name": "test_test_failure.py::test_error",
    }
    assert (
        spans["test_test_failure.py::test_error::setup"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["test_test_failure.py::test_error::call"].attributes == {
        "test.type": "test.call",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        "test.case.name": "test_test_failure.py::test_error",
    }
    assert (
        spans["test_test_failure.py::test_error::call"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["test_test_failure.py::test_error::teardown"].attributes == {
        "test.type": "test.teardown",
        "code.function": "test_error",
        "code.lineno": 0,
        "code.filepath": "test_test_failure.py",
        "test.case.name": "test_test_failure.py::test_error",
    }
    assert (
        spans["test_test_failure.py::test_error::teardown"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )


def test_fixture(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("""
    import pytest
    @pytest.fixture
    def myfix(): pass
    def test_pass(myfix): pass
    """)

    expected_spans = (
        "myfix setup",
        "myfix teardown",
    )
    for name in expected_spans:
        assert name in spans

    assert spans["myfix setup"].attributes == {
        "test.type": "fixture",
        "code.function": "myfix",
        "code.lineno": 2,
        "code.filepath": anys.ANY_STR,
        "test.fixture.scope": "function",
    }
    assert typing.cast(str, spans["myfix setup"].attributes["code.filepath"]).endswith(
        "test_fixture.py"
    )
    assert (
        spans["myfix setup"].status.status_code == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["myfix teardown"].attributes == {
        "test.type": "fixture",
        "code.function": "myfix",
        "code.lineno": 2,
        "code.filepath": anys.ANY_STR,
        "test.fixture.scope": "function",
    }
    assert typing.cast(
        str, spans["myfix teardown"].attributes["code.filepath"]
    ).endswith("test_fixture.py")
    assert (
        spans["myfix teardown"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )


def test_fixture_failure(
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    result, spans = pytester_with_spans("""
    import pytest
    @pytest.fixture
    def myfix(): raise Exception("HELLO")
    def test_pass(myfix): pass
    """)

    expected_spans = (
        "myfix setup",
        "myfix teardown",
    )
    for name in expected_spans:
        assert name in spans

    assert spans["myfix setup"].attributes == {
        "test.type": "fixture",
        "code.function": "myfix",
        "code.lineno": 2,
        "code.filepath": anys.ANY_STR,
        "test.fixture.scope": "function",
    }
    assert typing.cast(str, spans["myfix setup"].attributes["code.filepath"]).endswith(
        "test_fixture_failure.py"
    )
    # NOTE: this should probably be error, but it seems there no way to catch fixture issue
    # Anyhow, the test is marked as fail and the problem is pointed to the fixture
    assert (
        spans["myfix setup"].status.status_code == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["myfix teardown"].attributes == {
        "test.type": "fixture",
        "code.function": "myfix",
        "code.lineno": 2,
        "code.filepath": anys.ANY_STR,
        "test.fixture.scope": "function",
    }
    assert typing.cast(
        str, spans["myfix teardown"].attributes["code.filepath"]
    ).endswith("test_fixture_failure.py")
    assert (
        spans["myfix teardown"].status.status_code
        == opentelemetry.trace.StatusCode.UNSET
    )

    assert spans["test_fixture_failure.py::test_pass"].attributes == {
        "test.type": "test",
        "code.function": "test_pass",
        "code.lineno": 3,
        "code.filepath": "test_fixture_failure.py",
        "test.case.name": "test_fixture_failure.py::test_pass",
        SpanAttributes.EXCEPTION_TYPE: "<class 'Exception'>",
        SpanAttributes.EXCEPTION_MESSAGE: "HELLO",
        SpanAttributes.EXCEPTION_STACKTRACE: """@pytest.fixture
>   def myfix(): raise Exception("HELLO")
E   Exception: HELLO

test_fixture_failure.py:3: Exception""",
    }
    assert (
        spans["test_fixture_failure.py::test_pass"].status.status_code
        == opentelemetry.trace.StatusCode.ERROR
    )
    assert (
        spans["test_fixture_failure.py::test_pass"].status.description
        == "<class 'Exception'>: HELLO"
    )
