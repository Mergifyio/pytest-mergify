import dataclasses
import os
import random
import typing

import _pytest.nodes
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
from pytest_mergify import flaky_detection, utils


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

    flaky_detector: typing.Optional[flaky_detection.FlakyDetector] = dataclasses.field(
        init=False,
        default=None,
    )
    flaky_detector_error_message: typing.Optional[str] = dataclasses.field(
        init=False,
        default=None,
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

        self._load_flaky_detector()

        if self.token and self.repo_name and self.branch_name:
            self.quarantined_tests = pytest_mergify.quarantine.Quarantine(
                self.api_url,
                self.token,
                self.repo_name,
                self.branch_name,
            )

    def _load_flaky_detector(self) -> None:
        if (
            self.token is None
            or self.repo_name is None
            # NOTE(remyduthu): Hide behind a feature flag for now.
            or not utils.is_env_truthy("_MERGIFY_TEST_NEW_FLAKY_DETECTION")
        ):
            return

        try:
            self.flaky_detector = flaky_detection.FlakyDetector(
                token=self.token,
                url=self.api_url,
                full_repository_name=self.repo_name,
            )
        except Exception as exception:
            self.flaky_detector_error_message = (
                f"Could not load flaky detector: {str(exception)}"
            )

    def mark_test_as_quarantined_if_needed(self, item: _pytest.nodes.Item) -> bool:
        """
        Returns `True` if the test was marked as quarantined, otherwise returns `False`.
        """
        if self.quarantined_tests is not None and item in self.quarantined_tests:
            self.quarantined_tests.mark_test_as_quarantined(item)
            return True

        return False
