import os
import typing

import _pytest.main
import _pytest.config
import _pytest.config.argparsing
import _pytest.nodes
import _pytest.terminal

from opentelemetry import context
from opentelemetry.sdk.trace import export
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, Span
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)


import pytest_opentelemetry.instrumentation


CIProviderT = typing.Literal["github_actions", "circleci"]


class InterceptingSpanProcessor(SpanProcessor):
    trace_id: None | int

    def __init__(self) -> None:
        self.trace_id = None

    def on_start(
        self, span: Span, parent_context: context.Context | None = None
    ) -> None:
        if span.attributes is not None and any(
            "pytest" in attr for attr in span.attributes
        ):
            self.trace_id = span.context.trace_id


def is_running_in_ci() -> bool:
    return os.environ.get("CI") is not None


class PytestMergify:
    @staticmethod
    def get_ci_provider() -> CIProviderT | None:
        if os.getenv("GITHUB_ACTIONS") == "true":
            return "github_actions"
        if os.getenv("CIRCLECI") == "true":
            return "circleci"
        return None

    def ci_supports_trace_interception(self) -> bool:
        return self.get_ci_provider() == "github_actions"

    def pytest_configure(self, config: _pytest.config.Config) -> None:
        self.token = os.environ.get("MERGIFY_TOKEN")

        exporter: export.SpanExporter
        if os.environ.get("PYTEST_MERGIFY_DEBUG"):
            exporter = export.ConsoleSpanExporter()
        elif self.token:
            url = config.getoption("--mergify-api-url") or os.environ.get(
                "MERGIFY_API_URL", "https://api.mergify.com"
            )
            exporter = OTLPSpanExporter(
                endpoint=f"{url}/v1/ci/traces",
                headers={"Authorization": f"Bearer {self.token}"},
                compression=Compression.Gzip,
            )
        else:
            return

        tracer_provider = TracerProvider()

        span_processor = export.BatchSpanProcessor(exporter)
        tracer_provider.add_span_processor(span_processor)

        if self.ci_supports_trace_interception():
            self.interceptor = InterceptingSpanProcessor()
            tracer_provider.add_span_processor(self.interceptor)

        self.tracer = tracer_provider.get_tracer("pytest-mergify")
        # Replace tracer of pytest-opentelemetry
        pytest_opentelemetry.instrumentation.tracer = self.tracer

    def pytest_terminal_summary(
        self, terminalreporter: _pytest.terminal.TerminalReporter
    ) -> None:
        terminalreporter.section("Mergify CI")

        if self.token is None:
            terminalreporter.write_line(
                "No token configured for Mergify; test results will not be uploaded",
                yellow=True,
            )
            return

        if self.interceptor.trace_id is None:
            terminalreporter.write_line(
                "No trace id detected, this test run will not be attached to the CI job",
                yellow=True,
            )
        elif self.get_ci_provider() == "github_actions":
            terminalreporter.write_line(
                f"::notice title=Mergify CI::MERGIFY_TRACE_ID={self.interceptor.trace_id}",
            )


def pytest_addoption(parser: _pytest.config.argparsing.Parser) -> None:
    group = parser.getgroup("pytest-mergify", "Mergify support for pytest")
    group.addoption(
        "--mergify-api-url",
        default=None,
        help=(
            "URL of the Mergify API "
            "(or set via MERGIFY_API_URL environment variable)",
        ),
    )


def pytest_configure(config: _pytest.config.Config) -> None:
    if is_running_in_ci():
        config.pluginmanager.register(PytestMergify())
