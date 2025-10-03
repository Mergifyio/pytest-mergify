import datetime
import re
import typing

import pytest
import responses

from pytest_mergify import ci_insights

from . import conftest


def _set_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_MERGIFY_TEST_NEW_FLAKY_DETECTION", "true")
    monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_API_URL", "https://example.com")
    monkeypatch.setenv("MERGIFY_TOKEN", "my_token")


def _make_quarantine_mock() -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/repositories/pytest-mergify/quarantines",
        json={"quarantined_tests": []},
        status=200,
    )


def _make_test_names_mock(test_names: typing.List[str] = [], status: int = 200) -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/tests/names",
        json={"test_names": test_names},
        status=status,
    )


def _make_test_client() -> ci_insights.MergifyCIInsights:
    return ci_insights.MergifyCIInsights(
        token="my_token",
        repo_name="Mergifyio/pytest-mergify",
        api_url="https://example.com",
    )


@responses.activate
def test_load_flaky_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(test_names=["a::test_a", "b::test_b"])

    client = _make_test_client()
    assert not client._flaky_detection_error_message
    assert client._existing_test_names == ["a::test_a", "b::test_b"]


@responses.activate
def test_load_flaky_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_test_names_mock(status=500)

    client = _make_test_client()
    assert not client._existing_test_names
    assert client._flaky_detection_error_message is not None
    assert "500 Server Error" in client._flaky_detection_error_message


@responses.activate
def test_flaky_detection(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_test_names_mock(
        [
            "test_flaky_detection.py::test_foo",
            "test_flaky_detection.py::test_unknown",
        ]
    )

    result, spans = pytester_with_spans(
        code="""
        import pytest

        def test_foo():
            assert True

        execution_count = 0

        def test_bar():
            # Simulate a flaky test.
            global execution_count
            execution_count += 1

            if execution_count == 1:
                pytest.fail("I'm flaky!")

        def test_baz():
            assert True

        def test_qux():
            pytest.skip("I'm skipped!")
        """
    )

    outcomes = result.parseoutcomes()

    # We can't predict the exact number because it depends on the time it takes
    # to run the tests. We just want to make sure that the tests are tested
    # multiple time.
    assert outcomes["passed"] > 1000

    # Only the first run of the flaky test.
    assert outcomes["failed"] == 1

    # The skipped test is tested only once because skipped tests are excluded from the flaky detection.
    assert outcomes["skipped"] == 1

    assert re.search(
        r"""🐛 Flaky detection
- Used [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
- Active for 2 new test\(s\):
    • 'test_flaky_detection\.py::test_bar' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection\.py::test_baz' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)""",
        result.stdout.str(),
        re.MULTILINE,
    )

    assert spans is not None

    # 1 span for the session and one per test.
    assert len(spans) == 1 + sum(outcomes.values())

    for test_name, expected in {
        "test_flaky_detection.py::test_foo": False,
        "test_flaky_detection.py::test_bar": True,
        "test_flaky_detection.py::test_baz": True,
        "test_flaky_detection.py::test_qux": False,
    }.items():
        span = spans.get(test_name)

        assert span is not None
        assert span.attributes is not None
        if expected:
            assert span.attributes.get("cicd.test.new")


@pytest.mark.parametrize(
    argnames=["budget_duration", "test_durations", "expected_allocation"],
    argvalues=[
        pytest.param(
            datetime.timedelta(seconds=30),
            {
                "test_a": datetime.timedelta(milliseconds=10),
                "test_b": datetime.timedelta(milliseconds=20),
                "test_c": datetime.timedelta(milliseconds=30),
                "test_d": datetime.timedelta(milliseconds=40),
            },
            {
                "test_a": 750,
                "test_b": 375,
                "test_c": 250,
                "test_d": 187,
            },
            id="Basic",
        ),
        pytest.param(
            datetime.timedelta(seconds=10),
            {
                "test_a": datetime.timedelta(milliseconds=10),
                "test_b": datetime.timedelta(milliseconds=2),
                "test_c": datetime.timedelta(milliseconds=100),
                "test_d": datetime.timedelta(seconds=10),  # Not affordable.
                "test_e": datetime.timedelta(milliseconds=1),
                "test_f": datetime.timedelta(milliseconds=100),
            },
            {
                "test_a": 233,
                "test_b": ci_insights._MAX_TEST_RETRY_COUNT,
                "test_c": 23,
                "test_e": ci_insights._MAX_TEST_RETRY_COUNT,
                "test_f": 23,
            },
            id="With slow and fast tests",
        ),
        pytest.param(
            datetime.timedelta(seconds=10),
            {},
            {},
            id="Empty test durations",
        ),
        pytest.param(
            datetime.timedelta(),
            {"test_a": datetime.timedelta(milliseconds=1)},
            {},
            id="Empty budget duration",
        ),
        pytest.param(
            datetime.timedelta(milliseconds=1),
            {
                "test_a": datetime.timedelta(seconds=1),
                "test_b": datetime.timedelta(seconds=2),
            },
            {},
            id="Budget too small",
        ),
        pytest.param(
            datetime.timedelta(seconds=5),
            {"test_a": datetime.timedelta(seconds=1)},
            {"test_a": 5},
            id="Exact budget for minimum retries",
        ),
        pytest.param(
            datetime.timedelta(seconds=2000),
            {"test_a": datetime.timedelta(milliseconds=1)},
            {"test_a": 1000},
            id="Test reaching maximum retries",
        ),
    ],
)
def test_allocate_test_retries(
    budget_duration: datetime.timedelta,
    test_durations: typing.Dict[str, datetime.timedelta],
    expected_allocation: typing.Dict[str, int],
) -> None:
    allocation = ci_insights._allocate_test_retries(budget_duration, test_durations)

    total_duration = datetime.timedelta()

    for test, retry_count in allocation.items():
        assert test in test_durations
        assert retry_count >= ci_insights._MIN_TEST_RETRY_COUNT
        assert retry_count <= ci_insights._MAX_TEST_RETRY_COUNT

        total_duration += test_durations[test] * retry_count

    # We want to make sure that we are never consuming more than the allocated
    # budget.
    assert total_duration <= budget_duration

    assert allocation == expected_allocation
