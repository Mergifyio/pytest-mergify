import datetime
import typing

import _pytest
import _pytest.reports
import freezegun
import pytest

import pytest_mergify
from pytest_mergify import flaky_detection
from pytest_mergify import utils

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
        self._over_length_tests = set()
        self._available_budget_duration = datetime.timedelta()
        self._tests_to_process = []
        self._suspended_item_finalizers = {}
        self._debug_logs = []
        self._is_xdist = False

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


def test_flaky_detector_to_serializable_metrics() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(max_test_name_length=100)
    detector._test_metrics = {
        "test_foo": flaky_detection._TestMetrics(
            initial_setup_duration=datetime.timedelta(milliseconds=100),
            initial_call_duration=datetime.timedelta(milliseconds=200),
            initial_teardown_duration=datetime.timedelta(milliseconds=50),
            rerun_count=3,
            prevented_timeout=True,
            total_duration=datetime.timedelta(milliseconds=1050),
        ),
    }
    detector._over_length_tests = {"test_long_name"}
    detector._debug_logs = [
        utils.StructuredLog.make(message="test log", key="value"),
    ]

    result = detector.to_serializable_metrics()

    assert result["test_metrics"]["test_foo"]["rerun_count"] == 3
    assert result["test_metrics"]["test_foo"]["total_duration_ms"] == 1050.0
    assert result["test_metrics"]["test_foo"]["initial_setup_duration_ms"] == 100.0
    assert result["test_metrics"]["test_foo"]["initial_call_duration_ms"] == 200.0
    assert result["test_metrics"]["test_foo"]["initial_teardown_duration_ms"] == 50.0
    assert result["test_metrics"]["test_foo"]["prevented_timeout"] is True
    assert result["over_length_tests"] == ["test_long_name"]
    assert len(result["debug_logs"]) == 1
    assert result["debug_logs"][0]["message"] == "test log"


def test_flaky_detector_from_context() -> None:
    context_dict = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_foo", "test_bar"],
        "existing_tests_mean_duration_ms": 5000,
        "unhealthy_test_names": ["test_foo"],
        "max_test_execution_count": 100,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }

    detector = flaky_detection.FlakyDetector.from_context(
        context_dict=context_dict,
        mode="new",
    )

    assert detector.mode == "new"
    assert detector._context.existing_test_names == ["test_foo", "test_bar"]
    assert detector._context.existing_tests_mean_duration_ms == 5000
    assert detector._context.max_test_execution_count == 100
    assert detector._test_metrics == {}
    assert detector._tests_to_process == []


def test_make_report_from_aggregated() -> None:
    context_dict = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_existing"],
        "existing_tests_mean_duration_ms": 10000,
        "unhealthy_test_names": [],
        "max_test_execution_count": 1000,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }
    metrics = {
        "test_metrics": {
            "test_bar": {
                "rerun_count": 10,
                "total_duration_ms": 1000.0,
                "initial_setup_duration_ms": 10.0,
                "initial_call_duration_ms": 80.0,
                "initial_teardown_duration_ms": 10.0,
                "prevented_timeout": False,
            },
        },
        "over_length_tests": [],
        "debug_logs": [],
    }

    report = flaky_detection.make_report_from_aggregated(
        context_dict=context_dict,
        mode="new",
        available_budget_duration_ms=4000.0,
        aggregated_metrics=metrics,
    )

    assert "Flaky detection" in report
    assert "test_bar" in report
    assert "has been tested 10 time" in report
    assert "Active for 1 new test" in report


@freezegun.freeze_time(time_to_freeze=_NOW)
def test_flaky_detector_set_test_deadline_static() -> None:
    """Under xdist, deadlines use static per-test budget allocation."""
    detector = InitializedFlakyDetector()
    detector.mode = "new"
    detector._is_xdist = True
    detector._context = _make_flaky_detection_context()
    detector._available_budget_duration = datetime.timedelta(seconds=10)
    detector._tests_to_process = ["foo", "bar"]
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(),
    }

    detector.set_test_deadline(test="foo")

    metrics = detector._test_metrics["foo"]
    assert metrics.deadline is not None
    # Static: 10s / 2 tests = 5s per test.
    expected_deadline = _NOW + datetime.timedelta(seconds=5)
    assert metrics.deadline == expected_deadline


def test_make_report_from_aggregated_no_tests() -> None:
    context_dict = {
        "budget_ratio_for_new_tests": 0.1,
        "budget_ratio_for_unhealthy_tests": 0.05,
        "existing_test_names": ["test_existing"],
        "existing_tests_mean_duration_ms": 10000,
        "unhealthy_test_names": [],
        "max_test_execution_count": 1000,
        "max_test_name_length": 65536,
        "min_budget_duration_ms": 4000,
        "min_test_execution_count": 5,
    }

    report = flaky_detection.make_report_from_aggregated(
        context_dict=context_dict,
        mode="new",
        available_budget_duration_ms=4000.0,
        aggregated_metrics={
            "test_metrics": {},
            "over_length_tests": [],
            "debug_logs": [],
        },
    )

    assert "No new tests detected" in report
