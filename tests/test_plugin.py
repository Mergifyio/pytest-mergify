import pytest

import _pytest.config
from _pytest.pytester import Pytester

import pytest_mergify


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
    pytester: Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    monkeypatch.setenv("MERGIFY_TOKEN", "foobar")
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
    pytester: Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Mergifyio/pytest-mergify")
    plugin = pytest_mergify.PytestMergify()
    pytester.makepyfile("")
    pytester.runpytest_inprocess(plugins=[plugin])
    assert plugin.mergify_tracer.repo_name == "Mergifyio/pytest-mergify"


def test_with_token_no_ci_provider(
    pytester: Pytester,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERGIFY_TOKEN", "x")
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This will try to upload traces, but we don't have a real exporter so it will log errors.
    monkeypatch.setenv("MERGIFY_TOKEN", "x")
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "foo/bar")
    pytester.makepyfile(
        """
        def test_pass():
            pass
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
