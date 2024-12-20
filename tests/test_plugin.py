import pytest

import _pytest.config
from _pytest.pytester import Pytester

import pytest_mergify

from tests import conftest


def test_plugin_is_loaded(pytestconfig: _pytest.config.Config) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("pytest_mergify")
    assert plugin is pytest_mergify

    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert isinstance(plugin, pytest_mergify.PytestMergify)


def test_no_token(pytester: Pytester) -> None:
    pytester.makepyfile(
        """
        def test_foo():
            assert True
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert (
        "No token configured for Mergify; test results will not be uploaded"
        in result.stdout.lines
    )


def test_with_token_gha(
    pytester: Pytester, reconfigure_mergify_tracer: conftest.ReconfigureT
) -> None:
    reconfigure_mergify_tracer(
        {
            "CI": "1",
            "GITHUB_REPOSITORY": "Mergifyio/pytest-mergify",
            "MERGIFY_TOKEN": "foobar",
            "GITHUB_ACTIONS": "true",
        },
    )
    pytester.makepyfile(
        """
        def test_foo():
            assert True
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    for line in result.stdout.lines:
        if line.startswith("::notice title=Mergify CI::MERGIFY_TRACE_ID="):
            notice, title, trace_id = line.split("=", 2)
            int(trace_id)
            break
    else:
        pytest.fail("No trace id found")


def test_repo_name_github_actions(
    pytestconfig: _pytest.config.Config,
    reconfigure_mergify_tracer_gha: None,
) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    assert plugin.mergify_tracer.repo_name == "Mergifyio/pytest-mergify"


def test_with_token_no_ci_provider(
    pytester: Pytester,
    reconfigure_mergify_tracer: conftest.ReconfigureT,
) -> None:
    reconfigure_mergify_tracer(
        {"MERGIFY_TOKEN": "x", "CI": "1", "GITHUB_ACTIONS": "false"}
    )
    pytester.makepyfile(
        """
        def test_foo():
            assert True
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert (
        "Unable to determine repository name; test results will not be uploaded"
        in result.stdout.lines
    )


def test_errors_logs(
    pytester: _pytest.pytester.Pytester,
    reconfigure_mergify_tracer: conftest.ReconfigureT,
) -> None:
    # This will try to upload traces, but we don't have a real exporter so it will log errors.
    reconfigure_mergify_tracer(
        {
            "MERGIFY_TOKEN": "true",
            "CI": "1",
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "foo/bar",
        }
    )
    pytester.makepyfile(
        """
        import pytest

        from pytest_mergify import utils

        def test_span(pytestconfig):
            plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
            assert plugin is not None
            assert plugin.mergify_tracer.exporter is not None
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert any(
        line.startswith(
            "Error while exporting traces: HTTPConnectionPool(host='localhost', port=9999): Max retries exceeded with url"
        )
        for line in result.stdout.lines
    )
