import datetime
import typing

import _pytest
import _pytest.reports
import freezegun
import pytest

import pytest_mergify
from pytest_mergify import flaky_detection

_NOW = datetime.datetime(
    year=2025,
    month=1,
    day=1,
    hour=0,
    minute=0,
    second=0,
    tzinfo=datetime.timezone.utc,
)


class InitializedFlakyDetector(flaky_detection.FlakyDetector):
    def __init__(self) -> None:
        self.token = ""
        self.url = ""
        self.full_repository_name = ""
        self.mode = "new"
        self._test_metrics = {}

    def __post_init__(self) -> None:
        pass


def _make_flaky_detection_context(
    budget_ratio_for_new_tests: float = 0,
    budget_ratio_for_unhealthy_tests: float = 0,
    existing_test_names: typing.List[str] = [],
    existing_tests_mean_duration_ms: int = 0,
    unhealthy_test_names: typing.List[str] = [],
    max_test_execution_count: int = 0,
    max_test_name_length: int = 0,
    min_budget_duration_ms: int = 0,
    min_test_execution_count: int = 0,
) -> flaky_detection._FlakyDetectionContext:
    return flaky_detection._FlakyDetectionContext(
        budget_ratio_for_new_tests=budget_ratio_for_new_tests,
        budget_ratio_for_unhealthy_tests=budget_ratio_for_unhealthy_tests,
        existing_test_names=existing_test_names,
        existing_tests_mean_duration_ms=existing_tests_mean_duration_ms,
        unhealthy_test_names=unhealthy_test_names,
        max_test_execution_count=max_test_execution_count,
        max_test_name_length=max_test_name_length,
        min_budget_duration_ms=min_budget_duration_ms,
        min_test_execution_count=min_test_execution_count,
    )


def test_flaky_detector_try_fill_metrics_from_report() -> None:
    def make_report(
        nodeid: str, when: typing.Literal["setup", "call", "teardown"], duration: float
    ) -> _pytest.reports.TestReport:
        return _pytest.reports.TestReport(
            duration=duration,
            keywords={},
            location=("", None, ""),
            longrepr=None,
            nodeid=nodeid,
            outcome="passed",
            when=when,
        )

    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(max_test_name_length=100)
    detector._tests_to_process = ["foo"]

    plugin = pytest_mergify.PytestMergify()
    plugin.mergify_ci = pytest_mergify.ci_insights.MergifyCIInsights()
    plugin.mergify_ci.flaky_detector = detector

    plugin.pytest_runtest_logreport(make_report(nodeid="foo", when="setup", duration=1))
    plugin.pytest_runtest_logreport(make_report(nodeid="foo", when="call", duration=2))
    plugin.pytest_runtest_logreport(
        make_report(nodeid="foo", when="teardown", duration=3)
    )

    plugin.pytest_runtest_logreport(make_report(nodeid="foo", when="setup", duration=4))
    plugin.pytest_runtest_logreport(make_report(nodeid="foo", when="call", duration=5))
    plugin.pytest_runtest_logreport(
        make_report(nodeid="foo", when="teardown", duration=6)
    )

    metrics = detector._test_metrics.get("foo")
    assert metrics is not None
    assert metrics.initial_duration == datetime.timedelta(seconds=6)
    assert metrics.rerun_count == 2
    assert metrics.total_duration == datetime.timedelta(seconds=21)


def test_flaky_detector_count_remaining_tests() -> None:
    detector = InitializedFlakyDetector()
    detector.mode = "new"
    detector._tests_to_process = ["foo", "bar", "baz"]
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(
            deadline=datetime.datetime.now(datetime.timezone.utc)
        ),
        "bar": flaky_detection._TestMetrics(),
        "baz": flaky_detection._TestMetrics(),
    }
    assert detector._count_remaining_tests() == 2


@freezegun.freeze_time(
    time_to_freeze=datetime.datetime.fromisoformat("2025-01-01T00:00:00+00:00")
)
@pytest.mark.parametrize(
    argnames=("metrics", "expected"),
    argvalues=[
        pytest.param(flaky_detection._TestMetrics(), True, id="Deadline not set"),
        pytest.param(
            flaky_detection._TestMetrics(
                deadline=datetime.datetime.fromisoformat("2025-01-02T00:00:00+00:00"),
                initial_call_duration=datetime.timedelta(seconds=1),
            ),
            False,
            id="Not exceeded",
        ),
        pytest.param(
            flaky_detection._TestMetrics(
                deadline=datetime.datetime.fromisoformat("2025-01-01T00:00:00+00:00"),
                initial_call_duration=datetime.timedelta(),
            ),
            True,
            id="Exceeded by deadline",
        ),
        pytest.param(
            flaky_detection._TestMetrics(
                deadline=datetime.datetime.fromisoformat("2025-01-01T00:00:00+00:00"),
                initial_call_duration=datetime.timedelta(minutes=2),
            ),
            True,
            id="Exceeded by initial duration",
        ),
    ],
)
def test_flaky_detector_will_exceed_test_deadline(
    metrics: flaky_detection._TestMetrics,
    expected: bool,
) -> None:
    assert metrics.will_exceed_deadline() == expected


@pytest.mark.parametrize(
    argnames=(
        "available_budget_duration",
        "test_metrics",
        "expected",
    ),
    argvalues=[
        pytest.param(
            datetime.timedelta(seconds=1),
            {
                "baz": flaky_detection._TestMetrics(
                    total_duration=datetime.timedelta(milliseconds=500)
                ),
            },
            # Total test duration: 2 tests × 2000 ms = 4 s
            # Flaky detection budget: 4 s × 0.25 = 1 s
            # Already used: 500 ms (baz's `total_duration`)
            # Remaining budget: 1 s - 500 ms = 500 ms
            datetime.timedelta(milliseconds=500),
            id="Simple",
        ),
        pytest.param(
            datetime.timedelta(milliseconds=400),
            {
                "baz": flaky_detection._TestMetrics(
                    total_duration=datetime.timedelta(milliseconds=500)
                ),
            },
            datetime.timedelta(),
            id="No more budget",
        ),
    ],
)
def test_flaky_detector_get_remaining_budget_duration(
    available_budget_duration: datetime.timedelta,
    test_metrics: typing.Dict[str, flaky_detection._TestMetrics],
    expected: datetime.timedelta,
) -> None:
    detector = InitializedFlakyDetector()
    detector._available_budget_duration = available_budget_duration
    detector._test_metrics = test_metrics
    assert expected == detector._get_remaining_budget_duration()
