import pytest

import _pytest.config
from _pytest.pytester import Pytester

import pytest_mergify

pytest_plugins = ["pytester"]


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


def test_with_token_gha(pytester: Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERGIFY_TOKEN", "foobar")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("MERGIFY_API_URL", "https://localhost/v1/ci/traces")

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


def test_with_token_no_ci_provider(
    pytester: Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERGIFY_TOKEN", "foobar")
    pytester.makepyfile(
        """
        def test_foo():
            assert True
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert "Nothing to do" in result.stdout.lines


def test_repo_name(pytestconfig: _pytest.config.Config) -> None:
    plugin = pytestconfig.pluginmanager.get_plugin("PytestMergify")
    assert plugin is not None
    assert plugin.repo_name == "Mergifyio/pytest-mergify"
