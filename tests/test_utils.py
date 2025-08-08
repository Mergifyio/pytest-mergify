import pytest

from pytest_mergify.utils import get_repository_name_from_url


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
