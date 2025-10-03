import dataclasses
import datetime
import os
import random
import typing

import _pytest.main
import _pytest.nodes
import _pytest.terminal
import opentelemetry.sdk.resources
import requests
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider, export
from opentelemetry.semconv._incubating.attributes import vcs_attributes

import pytest_mergify.quarantine
import pytest_mergify.resources.ci as resources_ci
import pytest_mergify.resources.git as resources_git
import pytest_mergify.resources.github_actions as resources_gha
import pytest_mergify.resources.jenkins as resources_jenkins
import pytest_mergify.resources.mergify as resources_mergify
import pytest_mergify.resources.pytest as resources_pytest
from pytest_mergify import utils


class SynchronousBatchSpanProcessor(export.SimpleSpanProcessor):
    def __init__(self, exporter: export.SpanExporter) -> None:
        super().__init__(exporter)
        self.queue: typing.List[ReadableSpan] = []

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.span_exporter.export(self.queue)
        self.queue.clear()
        return True

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return

        self.queue.append(span)


class SessionHardRaiser(requests.Session):  # type: ignore[misc]
    """Custom requests.Session that raises an exception on HTTP error."""

    def request(self, *args: typing.Any, **kwargs: typing.Any) -> requests.Response:
        response = super().request(*args, **kwargs)
        response.raise_for_status()
        return response


# NOTE(remyduthu): We are using a hard-coded budget for now, but the idea is to
# make it configurable in the future.
_DEFAULT_TEST_RETRY_BUDGET_RATIO = 0.1
_MAX_TEST_RETRY_COUNT = 1000
_MIN_TEST_RETRY_BUDGET_DURATION = datetime.timedelta(seconds=1)


@dataclasses.dataclass
class MergifyCIInsights:
    token: typing.Optional[str] = dataclasses.field(
        default_factory=lambda: os.environ.get("MERGIFY_TOKEN")
    )
    repo_name: typing.Optional[str] = dataclasses.field(
        default_factory=utils.get_repository_name
    )
    api_url: str = dataclasses.field(
        default_factory=lambda: os.environ.get(
            "MERGIFY_API_URL", "https://api.mergify.com"
        )
    )
    branch_name: typing.Optional[str] = dataclasses.field(
        init=False,
        default=None,
    )
    exporter: typing.Optional[export.SpanExporter] = dataclasses.field(
        init=False, default=None
    )
    tracer: typing.Optional[opentelemetry.trace.Tracer] = dataclasses.field(
        init=False, default=None
    )
    tracer_provider: typing.Optional[opentelemetry.sdk.trace.TracerProvider] = (
        dataclasses.field(init=False, default=None)
    )
    test_run_id: str = dataclasses.field(
        init=False,
        default_factory=lambda: random.getrandbits(64).to_bytes(8, "big").hex(),
    )
    _existing_test_names: typing.List[str] = dataclasses.field(
        init=False,
        default_factory=list,
    )
    _flaky_detection_error_message: typing.Optional[str] = dataclasses.field(
        init=False,
        default=None,
    )
    _total_test_durations_ms: int = dataclasses.field(init=False, default=0)
    _new_test_durations_by_name: typing.Dict[str, int] = dataclasses.field(
        init=False, default_factory=dict
    )
    _new_test_retry_count_by_name: typing.DefaultDict[str, int] = dataclasses.field(
        init=False, default_factory=lambda: typing.DefaultDict(int)
    )
    quarantined_tests: typing.Optional[pytest_mergify.quarantine.Quarantine] = (
        dataclasses.field(
            init=False,
            default=None,
        )
    )

    def __post_init__(self) -> None:
        if not utils.is_in_ci():
            return

        span_processor: SpanProcessor

        if os.environ.get("PYTEST_MERGIFY_DEBUG"):
            self.exporter = export.ConsoleSpanExporter()
            span_processor = SynchronousBatchSpanProcessor(self.exporter)
        elif utils.strtobool(os.environ.get("_PYTEST_MERGIFY_TEST", "false")):
            from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
                InMemorySpanExporter,
            )

            self.exporter = InMemorySpanExporter()
            span_processor = export.SimpleSpanProcessor(self.exporter)
        elif self.token and self.repo_name:
            try:
                owner, repo = utils.split_full_repo_name(self.repo_name)
            except utils.InvalidRepositoryFullNameError:
                return
            self.exporter = OTLPSpanExporter(
                session=SessionHardRaiser(),
                endpoint=f"{self.api_url}/v1/ci/{owner}/repositories/{repo}/traces",
                headers={"Authorization": f"Bearer {self.token}"},
                compression=Compression.Gzip,
            )
            span_processor = SynchronousBatchSpanProcessor(self.exporter)
        else:
            return

        resource = opentelemetry.sdk.resources.get_aggregated_resources(
            [
                resources_git.GitResourceDetector(),
                resources_ci.CIResourceDetector(),
                resources_gha.GitHubActionsResourceDetector(),
                resources_jenkins.JenkinsResourceDetector(),
                resources_pytest.PytestResourceDetector(),
                resources_mergify.MergifyResourceDetector(),
            ]
        )

        resource = resource.merge(
            opentelemetry.sdk.resources.Resource(
                {
                    "test.run.id": self.test_run_id,
                }
            )
        )

        self.tracer_provider = TracerProvider(resource=resource)

        self.tracer_provider.add_span_processor(span_processor)
        self.tracer = self.tracer_provider.get_tracer("pytest-mergify")

        # Retrieve the branch name based on the detected resources's attributes
        branch_name = resource.attributes.get(
            vcs_attributes.VCS_REF_BASE_NAME,
            resource.attributes.get(vcs_attributes.VCS_REF_HEAD_NAME),
        )
        if branch_name is not None:
            # `str` cast just for `mypy`
            self.branch_name = str(branch_name)

        self._load_flaky_detection()

        if self.token and self.repo_name and self.branch_name:
            self.quarantined_tests = pytest_mergify.quarantine.Quarantine(
                self.api_url,
                self.token,
                self.repo_name,
                self.branch_name,
            )

    def _add_new_test_duration(self, test_name: str, test_duration_ms: int) -> None:
        if test_name in self._new_test_durations_by_name:
            return

        self._new_test_durations_by_name[test_name] = test_duration_ms

    def _is_flaky_detection_enabled(self) -> bool:
        return (
            self.token is not None
            and self.repo_name is not None
            # NOTE(remyduthu): Hide behind a feature flag for now.
            and utils.is_env_truthy("_MERGIFY_TEST_NEW_FLAKY_DETECTION")
        )

    def _is_flaky_detection_active(self) -> bool:
        return (
            self._is_flaky_detection_enabled()
            and self._flaky_detection_error_message is None
        )

    def _load_flaky_detection(self) -> None:
        if not self._is_flaky_detection_enabled():
            return

        try:
            self._existing_test_names = self._fetch_existing_test_names()
        except Exception as exception:
            self._flaky_detection_error_message = (
                f"Could not fetch existing test names: {str(exception)}"
            )

    def _fetch_existing_test_names(self) -> typing.List[str]:
        if not self.token or not self.repo_name or not self.branch_name:
            raise ValueError("'token', 'repo_name' and 'branch_name' are required")

        owner, repository = utils.split_full_repo_name(self.repo_name)

        response = requests.get(
            url=f"{self.api_url}/v1/ci/{owner}/tests/names",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"repository": repository, "branch": self.branch_name},
            timeout=10,
        )

        response.raise_for_status()

        return typing.cast(typing.List[str], response.json()["test_names"])

    def report_flaky_detection(
        self,
        terminalreporter: _pytest.terminal.TerminalReporter,
    ) -> None:
        if not self._is_flaky_detection_enabled():
            return

        if self._flaky_detection_error_message:
            terminalreporter.write_line(
                f"Unable to perform flaky detection. Error: {self._flaky_detection_error_message}",
                yellow=True,
            )

            return

        budget_duration_ms = int(self._get_budget_duration().total_seconds() * 1000)

        message = "🐛 Flaky detection"
        if self._new_test_durations_by_name:
            message += f"{os.linesep}- We applied flaky detection on {len(self._new_test_durations_by_name)} new test(s):"
            for test_name, retry_count in self._new_test_retry_count_by_name.items():
                test_retry_duration_ms = (
                    self._new_test_durations_by_name[test_name] * retry_count
                )

                message += (
                    f"{os.linesep}    • '{test_name}' has been tested {retry_count} "
                    f"times using approx. {test_retry_duration_ms / budget_duration_ms * 100:.1f} % "
                    f"of the budget ({test_retry_duration_ms} ms/{budget_duration_ms} ms)"
                )
        else:
            message += f"{os.linesep}- No new tests detected, but we are watching 👀"

        terminalreporter.write_line(message)

    def handle_flaky_detection_for_report(
        self,
        report: _pytest.reports.TestReport,
    ) -> None:
        if not self._is_flaky_detection_active():
            return

        if report.outcome not in ["failed", "passed"]:
            return

        test_duration_ms = int(report.duration * 1000)
        self._total_test_durations_ms += test_duration_ms

        test_name = report.nodeid
        if test_name in self._existing_test_names:
            return

        self._add_new_test_duration(test_name, test_duration_ms)

        if self.tracer:
            opentelemetry.trace.get_current_span().set_attributes(
                {"cicd.test.new": True}
            )

    def run_flaky_detection(self, session: _pytest.main.Session) -> None:
        if not self._is_flaky_detection_active():
            return

        new_items = [
            item
            for item in session.items
            if item.nodeid in self._new_test_durations_by_name
        ]

        budget_deadline = (
            datetime.datetime.now(datetime.timezone.utc) + self._get_budget_duration()
        )

        while datetime.datetime.now(datetime.timezone.utc) <= budget_deadline:
            items_to_retry = [
                item for item in new_items if self._should_retry_test(item.nodeid)
            ]
            if not items_to_retry:
                break

            # NOTE:
            #   We are running each test once at each iteration until the budget
            #   is exhausted.
            for item in items_to_retry:
                self._new_test_retry_count_by_name[item.nodeid] += 1
                item.ihook.pytest_runtest_protocol(item=item, nextitem=None)

    def _get_budget_duration(self) -> datetime.timedelta:
        """
        Calculate the budget duration based on a percentage of total test
        execution time.

        The budget ensures there's always a minimum time allocated of
        '_MIN_TEST_RETRY_BUDGET_DURATION_MS' even for very short test suites,
        preventing overly restrictive retry policies when the total test
        duration is small.
        """
        return max(
            datetime.timedelta(
                milliseconds=int(
                    _DEFAULT_TEST_RETRY_BUDGET_RATIO * self._total_test_durations_ms
                )
            ),
            _MIN_TEST_RETRY_BUDGET_DURATION,
        )

    def _should_retry_test(self, test_name: str) -> bool:
        """
        Determine if a test should be retried based on whether it has exceeded
        the maximum retry limit of `_MAX_TEST_RETRY_COUNT`.

        This check prevents really fast tests from being retried too many times
        which would be useless.
        """
        return (
            self._new_test_retry_count_by_name.get(test_name, 0)
            <= _MAX_TEST_RETRY_COUNT
        )

    def mark_test_as_quarantined_if_needed(self, item: _pytest.nodes.Item) -> bool:
        """
        Returns `True` if the test was marked as quarantined, otherwise returns `False`.
        """
        if self.quarantined_tests is not None and item in self.quarantined_tests:
            self.quarantined_tests.mark_test_as_quarantined(item)
            return True

        return False
