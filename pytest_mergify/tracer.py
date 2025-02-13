import dataclasses
import os

import opentelemetry.sdk.resources
from opentelemetry.sdk.trace import export
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, ReadableSpan
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)

from pytest_mergify import utils

import pytest_mergify.resources.ci as resources_ci
import pytest_mergify.resources.github_actions as resources_gha
import pytest_mergify.resources.pytest as resources_pytest
import pytest_mergify.resources.mergify as resources_mergify


class SynchronousBatchSpanProcessor(export.SimpleSpanProcessor):
    def __init__(self, exporter: export.SpanExporter) -> None:
        super().__init__(exporter)
        self.queue: list[ReadableSpan] = []

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.span_exporter.export(self.queue)
        self.queue.clear()
        return True

    def on_end(self, span: ReadableSpan) -> None:
        if not span.context.trace_flags.sampled:
            return

        self.queue.append(span)


@dataclasses.dataclass
class MergifyTracer:
    token: str | None = dataclasses.field(
        default_factory=lambda: os.environ.get("MERGIFY_TOKEN")
    )
    repo_name: str | None = dataclasses.field(default_factory=utils.get_repository_name)
    api_url: str = dataclasses.field(
        default_factory=lambda: os.environ.get(
            "MERGIFY_API_URL", "https://api.mergify.com"
        )
    )
    exporter: export.SpanExporter | None = dataclasses.field(init=False, default=None)
    tracer: opentelemetry.trace.Tracer | None = dataclasses.field(
        init=False, default=None
    )
    tracer_provider: opentelemetry.sdk.trace.TracerProvider | None = dataclasses.field(
        init=False, default=None
    )

    def __post_init__(self) -> None:
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
        elif self.token:
            if self.repo_name is None:
                return

            self.exporter = OTLPSpanExporter(
                endpoint=f"{self.api_url}/v1/repos/{self.repo_name}/ci/traces",
                headers={"Authorization": f"Bearer {self.token}"},
                compression=Compression.Gzip,
            )
            span_processor = SynchronousBatchSpanProcessor(self.exporter)
        else:
            return

        resources_gha.GitHubActionsResourceDetector().detect()
        resource = opentelemetry.sdk.resources.get_aggregated_resources(
            [
                resources_mergify.MergifyResourceDetector(),
                resources_ci.CIResourceDetector(),
                resources_gha.GitHubActionsResourceDetector(),
                resources_pytest.PytestResourceDetector(),
            ]
        )

        self.tracer_provider = TracerProvider(resource=resource)

        self.tracer_provider.add_span_processor(span_processor)
        self.tracer = self.tracer_provider.get_tracer("pytest-mergify")

    def ci_supports_trace_interception(self) -> bool:
        return utils.get_ci_provider() == "github_actions"
