import datetime
import re
import typing

import _pytest.nodes
import _pytest.pytester
import _pytest.reports
import pytest
import responses
from opentelemetry.sdk import trace

import pytest_mergify
from pytest_mergify import ci_insights
from tests import conftest


def _set_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    mode: typing.Literal["new", "unhealthy"] = "new",
) -> None:
    monkeypatch.setenv("_MERGIFY_TEST_NEW_FLAKY_DETECTION", "true")
    monkeypatch.setenv("_PYTEST_MERGIFY_TEST", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_API_URL", "https://example.com")
    monkeypatch.setenv("MERGIFY_TOKEN", "my_token")

    if mode == "unhealthy":
        # Simulate absence of a PR context: without `GITHUB_BASE_REF` and
        # `GITHUB_REF_NAME` `MergifyCIInsights.branch_name` can't be derived,
        # forcing the flaky detector to fall back to `unhealthy` mode. This
        # explicitly exercises the fallback path used when no PR metadata is
        # available.
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        monkeypatch.delenv("GITHUB_REF_NAME", raising=False)


def _make_quarantine_mock() -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/repositories/pytest-mergify/quarantines",
        json={"quarantined_tests": []},
        status=200,
    )


def _make_flaky_detection_context_mock(
    budget_ratio_for_new_tests: float = 0.1,
    budget_ratio_for_unhealthy_tests: float = 0.05,
    existing_test_names: typing.List[str] = [],
    existing_tests_mean_duration_ms: int = 0,
    unhealthy_test_names: typing.List[str] = [],
    max_test_execution_count: int = 1000,
    max_test_name_length: int = 65536,
    min_budget_duration_ms: int = 4000,
    min_test_execution_count: int = 5,
    status: int = 200,
) -> None:
    responses.add(
        method=responses.GET,
        url="https://example.com/v1/ci/Mergifyio/repositories/pytest-mergify/flaky-detection-context",
        json={
            "budget_ratio_for_new_tests": budget_ratio_for_new_tests,
            "budget_ratio_for_unhealthy_tests": budget_ratio_for_unhealthy_tests,
            "existing_test_names": existing_test_names,
            "existing_tests_mean_duration_ms": existing_tests_mean_duration_ms,
            "unhealthy_test_names": unhealthy_test_names,
            "max_test_execution_count": max_test_execution_count,
            "max_test_name_length": max_test_name_length,
            "min_budget_duration_ms": min_budget_duration_ms,
            "min_test_execution_count": min_test_execution_count,
        },
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
    _make_flaky_detection_context_mock(existing_test_names=["a::test_a", "b::test_b"])

    client = _make_test_client()
    assert not client.flaky_detector_error_message
    assert client.flaky_detector is not None
    assert client.flaky_detector._context.existing_test_names == [
        "a::test_a",
        "b::test_b",
    ]


@responses.activate
def test_load_flaky_detection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_flaky_detection_context_mock(status=500)

    client = _make_test_client()
    assert client.flaky_detector is None
    assert client.flaky_detector_error_message is not None
    assert "500 Server Error" in client.flaky_detector_error_message


@responses.activate
def test_load_flaky_detection_error_without_existing_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_test_environment(monkeypatch)

    _make_quarantine_mock()
    _make_flaky_detection_context_mock(existing_test_names=[])

    client = _make_test_client()
    assert client.flaky_detector is None
    assert client.flaky_detector_error_message is not None
    assert (
        "No existing tests found for 'Mergifyio/pytest-mergify' repository"
        in client.flaky_detector_error_message
    )


@responses.activate
def test_flaky_detection_for_new_tests(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    max_test_name_length = 100

    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_for_new_tests.py::test_foo",
            "test_flaky_detection_for_new_tests.py::test_unknown",
        ],
        max_test_name_length=max_test_name_length,
    )

    result, spans = pytester_with_spans(
        code=f"""
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

        def test_quux_{"a" * (max_test_name_length + 10)}():
            assert True

        def test_corge():
            assert True
        """
    )

    result.assert_outcomes(
        failed=1,  # Only the first execution of the flaky test.
        passed=3004,  # The initial execution of the 4 tests and 1000 executions for each new test.
        skipped=1,  # The skipped test is tested only once because skipped tests are excluded from the flaky detection.
    )

    assert re.search(
        r"""🐛 Flaky detection
- Skipped 1 test:
    • 'test_flaky_detection_for_new_tests\.py::test_quux_[a]+' has not been tested multiple times because the name of the test exceeds our limit of \d+ characters
- Used [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
- Active for 3 new tests:
    • 'test_flaky_detection_for_new_tests\.py::test_bar' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection_for_new_tests\.py::test_baz' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection_for_new_tests\.py::test_corge' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)""",
        result.stdout.str(),
        re.MULTILINE,
    )

    assert spans is not None

    # 1 span for the session and one per test.
    assert len(spans) == 1 + sum(result.parseoutcomes().values())

    new_tests = [
        "test_flaky_detection_for_new_tests.py::test_bar",
        "test_flaky_detection_for_new_tests.py::test_baz",
        "test_flaky_detection_for_new_tests.py::test_corge",
    ]
    for span in spans.values():
        assert span is not None
        assert span.attributes is not None

        is_new_test = span.name in new_tests
        assert span.attributes.get("cicd.test.new", False) == is_new_test


@responses.activate
def test_flaky_detection_for_unhealthy_tests(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch, mode="unhealthy")
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        unhealthy_test_names=[
            "test_flaky_detection_for_unhealthy_tests.py::test_bar",
            "test_flaky_detection_for_unhealthy_tests.py::test_baz",
            "test_flaky_detection_for_unhealthy_tests.py::test_qux",
            "test_flaky_detection_for_unhealthy_tests.py::test_quux",
            "test_flaky_detection_for_unhealthy_tests.py::test_unknown",
        ],
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

        def test_quux():
            assert True
        """
    )

    result.assert_outcomes(
        failed=1,  # Only the first execution of the flaky test.
        passed=3,  # Do not log the reruns so the CI doesn't fail. The goal is to send data to Mergify, not lock the CI of our users.
        skipped=1,  # The skipped test is tested only once because skipped tests are excluded from the flaky detection.
    )

    assert re.search(
        r"""🐛 Flaky detection
- Used [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
- Active for 3 unhealthy tests:
    • 'test_flaky_detection_for_unhealthy_tests\.py::test_bar' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection_for_unhealthy_tests\.py::test_baz' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)
    • 'test_flaky_detection_for_unhealthy_tests\.py::test_quux' has been tested \d+ times using approx\. [0-9.]+ % of the budget \([0-9.]+ s/[0-9.]+ s\)""",
        result.stdout.str(),
        re.MULTILINE,
    )

    assert spans is not None

    # 1 span for the session and one per test, including 1000 retries for each unhealthy test.
    assert len(spans) == 1 + 3005


@responses.activate
def test_flaky_detection_with_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    max_test_name_length = 100

    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_with_fixtures.py::test_first",
            "test_flaky_detection_with_fixtures.py::test_last",
        ],
        max_test_name_length=max_test_name_length,
    )

    result, spans = pytester_with_spans(
        code="""
        import pytest

        SESSION_ALREADY_SET = False

        SETUP_COUNT = 0
        TEARDOWN_COUNT = 0

        @pytest.fixture(scope="session", autouse=True)
        def _setup_session() -> None:
            global SESSION_ALREADY_SET
            if SESSION_ALREADY_SET:
                raise RuntimeError("This function should not be called twice")
            SESSION_ALREADY_SET = True

        @pytest.fixture(autouse=True)
        def _setup_test():
            global SETUP_COUNT, TEARDOWN_COUNT

            SETUP_COUNT += 1

            yield

            TEARDOWN_COUNT += 1

        def test_first():
            assert True

        # This is a new test.
        def test_second():
            assert True

        def test_last():
            # This test validates that fixtures are properly set up and torn down
            # during test retries. With 3 tests total (test_first, test_second, test_last)
            # where test_second is new and gets retried 1000 times:
            # - SETUP_COUNT should be 1003 (1 initial run per test + 1000 retries of test_second)
            # - TEARDOWN_COUNT should be 1002 (all tests complete except test_last which is currently running)
            # This ensures that function-scoped fixtures execute fresh for each retry,
            # while session-scoped fixtures run only once (validated by SESSION_ALREADY_SET).
            global SETUP_COUNT, TEARDOWN_COUNT
            assert SETUP_COUNT == 1003
            assert TEARDOWN_COUNT == 1002  # Teardown hasn't run yet for test_last.
        """
    )

    result.assert_outcomes(
        passed=1003,  # The initial execution of the 3 tests and 1000 executions for the new test.
    )


@responses.activate
def test_flaky_detection_with_only_one_new_test_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_with_only_one_new_test_at_the_end.py::test_foo",
        ]
    )

    result, spans = pytester_with_spans(
        code="""
        import pytest

        SESSION_ALREADY_SET = False

        @pytest.fixture(scope="session", autouse=True)
        def _setup_session() -> None:
            global SESSION_ALREADY_SET
            if SESSION_ALREADY_SET:
                raise RuntimeError("This function should not be called twice")
            SESSION_ALREADY_SET = True

        def test_foo():
            assert True

        def test_bar():
            assert True
        """
    )
    result.assert_outcomes(passed=1002)

    assert spans is not None

    # Only 1 execution for the existing test (`test_foo`), 1001 executions for
    # the new test (`test_bar`) and 1 additional span for the session.
    assert _get_span_counts(spans) == {
        "pytest session start": 1,
        "test_flaky_detection_with_only_one_new_test_at_the_end.py::test_foo": 1,
        "test_flaky_detection_with_only_one_new_test_at_the_end.py::test_bar": 1001,
    }


@responses.activate
def test_flaky_detection_clones_items(
    monkeypatch: pytest.MonkeyPatch,
    pytester_with_spans: conftest.PytesterWithSpanT,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=["test_flaky_detection_clones_items.py::test_foo"],
    )

    result, spans = pytester_with_spans(
        code="""
        import pytest

        @pytest.mark.asyncio
        async def test_bar():
            assert True
        """
    )
    result.assert_outcomes(passed=1001)

    assert spans is not None
    assert _get_span_counts(spans) == {
        "pytest session start": 1,
        "test_flaky_detection_clones_items.py::test_bar": 1001,
    }


@responses.activate
def test_flaky_detection_slow_test_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    "Test that a slow test is not retried when it can't reach 5 within the budget."
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_slow_test_not_retried.py::test_existing",
        ],
        min_test_execution_count=5,
    )

    class CustomPlugin:
        def pytest_runtest_makereport(
            self,
            item: _pytest.nodes.Item,
            call: _pytest.reports.TestReport,
        ) -> None:
            if call.when != "call":
                return

            if "test_slow" in item.nodeid:
                call.duration = 10.0  # Simulate a slow test.
            else:
                call.duration = 0.001

    pytester.makepyfile(
        """
        def test_existing():
            assert True

        def test_fast():
            assert True

        def test_slow():
            assert True
        """
    )

    result = pytester.runpytest_inprocess(
        plugins=[CustomPlugin(), pytest_mergify.PytestMergify()]
    )
    result.assert_outcomes(passed=1003)

    # `test_fast` should have been tested successfully.
    assert re.search(
        r"'test_flaky_detection_slow_test_not_retried\.py::test_fast' has been tested \d+ times",
        result.stdout.str(),
    )

    assert (
        "'test_flaky_detection_slow_test_not_retried.py::test_slow' is too slow to be tested at least 5 times within the budget"
        in result.stdout.str()
    )


@responses.activate
def test_flaky_detection_budget_deadline_stops_retries(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    """
    Test that retries are stopped when they would exceed the budget deadline.
    """
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detection_budget_deadline_stops_retries.py::test_existing",
        ]
    )

    class CustomPlugin:
        deadline_patched: bool = False
        execution_count: int = 0

        def pytest_runtest_call(self, item: _pytest.nodes.Item) -> None:
            plugin = None
            for existing in item.session.config.pluginmanager.get_plugins():
                if isinstance(existing, pytest_mergify.PytestMergify):
                    plugin = existing

            if not plugin or not plugin.mergify_ci.flaky_detector:
                return

            self.execution_count += 1

            # Simulate a slow execution that reaches the deadline.
            if not self.deadline_patched and self.execution_count == 10:
                # Set the deadline in the past to stop immediately.
                plugin.mergify_ci.flaky_detector._deadline = datetime.datetime.now(
                    datetime.timezone.utc
                ) - datetime.timedelta(hours=1)

                self.deadline_patched = True

    pytester.makepyfile(
        """
        def test_existing():
            assert True

        def test_new():
            assert True
        """
    )

    result = pytester.runpytest_inprocess(
        plugins=[pytest_mergify.PytestMergify(), CustomPlugin()]
    )

    # We should have:
    # - 1 execution of `test_existing`,
    # - 1 initial execution of `test_new`,
    # - Only 8 retries of `test_new` before the deadline is reached.
    result.assert_outcomes(passed=10)

    assert re.search(
        r"'test_flaky_detection_budget_deadline_stops_retries\.py::test_new' has been tested only \d+ times instead of \d+ times to avoid exceeding the budget",
        result.stdout.str(),
    )


@responses.activate
def test_flaky_detector_filter_context_tests_with_session(
    monkeypatch: pytest.MonkeyPatch,
    pytester: _pytest.pytester.Pytester,
) -> None:
    _set_test_environment(monkeypatch)
    _make_quarantine_mock()
    _make_flaky_detection_context_mock(
        existing_test_names=[
            "test_flaky_detector_filter_context_tests_with_session.py::test_foo",
            "test_flaky_detector_filter_context_tests_with_session.py::test_bar",
            "test_flaky_detector_filter_context_tests_with_session.py::test_baz",  # Unknown test, should be filtered.
        ],
        unhealthy_test_names=[
            "test_flaky_detector_filter_context_tests_with_session.py::test_foo",
            "test_flaky_detector_filter_context_tests_with_session.py::test_qux",  # Unknown test, should be filtered.
        ],
    )

    pytester.makepyfile(
        """
        def test_foo():
            assert True

        def test_bar():
            assert True
        """
    )

    plugin = pytest_mergify.PytestMergify()

    result = pytester.runpytest_inprocess(plugins=[plugin])
    result.assert_outcomes(passed=2)  # 2 existing tests.

    assert plugin.mergify_ci.flaky_detector is not None

    # Unknown test should have been filtered out after collection.
    assert len(plugin.mergify_ci.flaky_detector._context.existing_test_names) == 2
    assert len(plugin.mergify_ci.flaky_detector._context.unhealthy_test_names) == 1


def _get_span_counts(
    spans: typing.Dict[str, trace.ReadableSpan],
) -> typing.Dict[str, int]:
    result: typing.Dict[str, int] = {}
    for span in spans.values():
        result[span.name] = result.get(span.name, 0) + 1

    return result
