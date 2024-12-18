import collections.abc
import typing

import pytest

import _pytest.config

from pytest_mergify import tracer

pytest_plugins = ["pytester"]


ReconfigureT = typing.Callable[[dict[str, str]], None]


@pytest.fixture
def reconfigure_mergify_tracer(
    pytestconfig: _pytest.config.Config,
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Generator[ReconfigureT, None, None]:
    # Always override API
    monkeypatch.setenv("MERGIFY_API_URL", "http://localhost:9999")

    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    old_tracer: tracer.MergifyTracer = plugin.mergify_tracer

    def _reconfigure(env: dict[str, str]) -> None:
        # Set environment variables
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        plugin.reconfigure()

    yield _reconfigure
    if plugin.mergify_tracer.tracer_provider is not None:
        plugin.mergify_tracer.tracer_provider.shutdown()
    plugin.mergify_tracer = old_tracer


@pytest.fixture
def reconfigure_mergify_tracer_gha(
    reconfigure_mergify_tracer: ReconfigureT,
) -> None:
    reconfigure_mergify_tracer(
        {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "Mergifyio/pytest-mergify"}
    )
