import typing

import pytest
import _pytest.main
import _pytest.config
import _pytest.config.argparsing
import _pytest.nodes
import _pytest.terminal

from pytest_mergify import utils
from pytest_mergify.tracer import MergifyTracer


class PytestMergify:
    __name__ = "PytestMergify"

    mergify_tracer: MergifyTracer

    # Do this after pytest-opentelemetry has setup things
    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config: _pytest.config.Config) -> None:
        api_url = config.getoption("--mergify-api-url")
        if api_url is None:
            self.reconfigure()
        else:
            self.reconfigure(api_url=api_url)

    def reconfigure(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.mergify_tracer = MergifyTracer(*args, **kwargs)

    def pytest_terminal_summary(
        self, terminalreporter: _pytest.terminal.TerminalReporter
    ) -> None:
        terminalreporter.section("Mergify CI")

        if self.mergify_tracer.token is None:
            terminalreporter.write_line(
                "No token configured for Mergify; test results will not be uploaded",
                yellow=True,
            )
            return

        if self.mergify_tracer.interceptor is None:
            terminalreporter.write_line("Nothing to do")
        else:
            if self.mergify_tracer.interceptor.trace_id is None:
                terminalreporter.write_line(
                    "No trace id detected, this test run will not be attached to the CI job",
                    yellow=True,
                )
            elif utils.get_ci_provider() == "github_actions":
                terminalreporter.write_line(
                    f"::notice title=Mergify CI::MERGIFY_TRACE_ID={self.mergify_tracer.interceptor.trace_id}",
                )


def pytest_addoption(parser: _pytest.config.argparsing.Parser) -> None:
    group = parser.getgroup("pytest-mergify", "Mergify support for pytest")
    group.addoption(
        "--mergify-api-url",
        help=(
            "URL of the Mergify API "
            "(or set via MERGIFY_API_URL environment variable)",
        ),
    )


def pytest_configure(config: _pytest.config.Config) -> None:
    config.pluginmanager.register(PytestMergify())
