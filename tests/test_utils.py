import pytest

from pytest_mergify.utils import get_repository_name_from_url, is_in_ci


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://github.com/owner/repo", "owner/repo"),
        ("https://gitlab.com/owner/repo", "owner/repo"),
        ("https://git.example.com/owner/repo", "owner/repo"),
        ("owner/repo", "owner/repo"),
        ("https://github.com/my-org.name/my-repo.name", "my-org.name/my-repo.name"),
        ("https://git.example.com:8080/owner/repo", "owner/repo"),
        ("https://github.com/owner123/repo456", "owner123/repo456"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("git@gitlab.com:owner/repo.git", "owner/repo"),
        (
            "git@git.example.com:my-org.name/my-repo.name.git",
            "my-org.name/my-repo.name",
        ),
        ("git@bitbucket.org:owner123/repo456.git", "owner123/repo456"),
    ],
)
def test_get_repository_name_from_url_valid(url: str, expected: str) -> None:
    """Test valid URL formats that should extract repository names."""
    result = get_repository_name_from_url(url)
    assert result == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/issues",
        "https://github.com/owner",
        "",
        "not-a-url",
        "https://github.com/owner/repo?tab=readme",
    ],
)
def test_get_repository_name_from_url_invalid(url: str) -> None:
    """Test invalid URL formats that should return None."""
    result = get_repository_name_from_url(url)
    assert result is None


@pytest.mark.parametrize(
    argnames=("value", "expected"),
    argvalues=[
        pytest.param("true", True, id="boolean-true"),
        pytest.param("1", True, id="boolean-one"),
        pytest.param("false", False, id="boolean-false"),
        pytest.param("0", False, id="boolean-zero"),
        # Woodpecker and Drone set `CI` to their own name rather than a boolean.
        pytest.param("woodpecker", True, id="provider-name"),
        pytest.param("drone", True, id="other-provider-name"),
        # A workflow whose `env: CI: ${{ ... }}` resolved to nothing.
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="blank"),
        # A YAML block scalar keeps the newline its author did not intend.
        pytest.param("false ", False, id="boolean-false-with-trailing-space"),
        pytest.param(" 0", False, id="boolean-zero-with-leading-space"),
        pytest.param("true\n", True, id="boolean-true-with-newline"),
    ],
)
def test_is_in_ci_accepts_any_value(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("CI", value)
    monkeypatch.delenv("PYTEST_MERGIFY_ENABLE", raising=False)

    assert is_in_ci() is expected
