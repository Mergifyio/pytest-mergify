import datetime
import typing

import _pytest
import freezegun

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


@freezegun.freeze_time(_NOW)
def test_flaky_detection_try_write_test_deadline() -> None:
    detector = InitializedFlakyDetector()

    detector._deadline = _NOW + datetime.timedelta(seconds=10)
    detector._test_metrics["foo"] = flaky_detection._TestMetrics(
        scheduled_rerun_count=5,
        initial_call_duration=datetime.timedelta(seconds=1),
    )
    detector.try_write_test_deadline("foo")
    assert (
        str(detector._test_metrics["foo"].deadline)
        == "2025-01-01 00:00:06.250000+00:00"  # 5 reruns * 1 sec * 1.25 safety margin.
    )

    detector._deadline = _NOW + datetime.timedelta(seconds=3)
    detector.try_write_test_deadline("foo")
    assert (
        str(detector._test_metrics["foo"].deadline)
        == "2025-01-01 00:00:03+00:00"  # Sooner global deadline.
    )

    detector.try_write_test_deadline("foo", timeout=datetime.timedelta(seconds=2))
    assert (
        str(detector._test_metrics["foo"].deadline)
        == "2025-01-01 00:00:02+00:00"  # Sooner timeout.
    )


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_duration_before_deadline() -> None:
    detector = InitializedFlakyDetector()
    detector._deadline = _NOW + datetime.timedelta(seconds=10)

    assert detector._get_duration_before_deadline() == datetime.timedelta(seconds=10)


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

    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="setup", duration=1)
    )
    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="call", duration=2)
    )
    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="teardown", duration=3)
    )

    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="setup", duration=4)
    )
    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="call", duration=5)
    )
    detector.try_fill_metrics_from_report(
        make_report(nodeid="foo", when="teardown", duration=6)
    )

    metrics = detector._test_metrics.get("foo")
    assert metrics is not None
    assert metrics.initial_duration == datetime.timedelta(seconds=6)
    assert metrics.rerun_count == 2
    assert metrics.total_duration == datetime.timedelta(seconds=21)


def test_flaky_detector_count_remaining_tests() -> None:
    detector = InitializedFlakyDetector()
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(is_processed=True),
        "bar": flaky_detection._TestMetrics(),
        "baz": flaky_detection._TestMetrics(),
    }
    assert detector._count_remaining_tests() == 2


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_rerun_count_for_test() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=4000,
        max_test_execution_count=1000,
    )
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(
            initial_call_duration=datetime.timedelta(milliseconds=10),
            is_processed=True,
        ),
        "bar": flaky_detection._TestMetrics(
            initial_call_duration=datetime.timedelta(milliseconds=100),
        ),
        "baz": flaky_detection._TestMetrics(),
    }
    detector.set_deadline()

    assert detector.get_rerun_count_for_test("bar") == 20


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_rerun_count_for_test_with_slow_test() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=500,
        max_test_execution_count=1000,
    )
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(
            # Can't be reran 5 times within the budget.
            initial_call_duration=datetime.timedelta(seconds=1),
        ),
        "bar": flaky_detection._TestMetrics(
            # This test should not be impacted by the previous one.
            initial_call_duration=datetime.timedelta(milliseconds=1),
        ),
    }
    detector.set_deadline()

    assert detector.get_rerun_count_for_test("foo") == 0

    assert detector.get_rerun_count_for_test("bar") == 500


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_rerun_count_for_test_with_fast_test() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=4000,
        max_test_execution_count=1000,
    )
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(
            # Should only be reran 1000 times, freeing the rest of the budget for other tests.
            initial_call_duration=datetime.timedelta(milliseconds=1),
        ),
    }
    detector.set_deadline()

    assert detector.get_rerun_count_for_test("foo") == 1000


@freezegun.freeze_time(_NOW)
def test_flaky_detector_get_rerun_count_for_test_with_timeout() -> None:
    detector = InitializedFlakyDetector()
    detector._context = _make_flaky_detection_context(
        min_test_execution_count=5,
        min_budget_duration_ms=4000,
        max_test_execution_count=1000,
    )
    detector._test_metrics = {
        "foo": flaky_detection._TestMetrics(
            initial_call_duration=datetime.timedelta(milliseconds=4),
        ),
    }
    detector.set_deadline()

    # 4 ms * 1000 = 4000 ms budget, but with a timeout of 500 ms and a 10%
    # safety margin (500 * 0.9 = 450 ms), we can only rerun 112 times (450 ms /
    # 4 ms).
    assert (
        detector.get_rerun_count_for_test(
            "foo", timeout=datetime.timedelta(milliseconds=500)
        )
        == 112
    )
