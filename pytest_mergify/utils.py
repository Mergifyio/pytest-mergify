import os
import re
import typing

CIProviderT = typing.Literal["github_actions", "circleci", "pytest_mergify_suite"]

SUPPORTED_CIs: typing.Dict[str, CIProviderT] = {
    "GITHUB_ACTIONS": "github_actions",
    "CIRCLECI": "circleci",
    "_PYTEST_MERGIFY_TEST": "pytest_mergify_suite",
}


def is_in_ci() -> bool:
    return strtobool(os.environ.get("CI", "false")) or strtobool(
        os.environ.get("PYTEST_MERGIFY_ENABLE", "false")
    )


def get_ci_provider() -> typing.Optional[CIProviderT]:
    for envvar, name in SUPPORTED_CIs.items():
        if envvar in os.environ and strtobool(os.environ[envvar]):
            return name

    return None


def get_repository_name() -> typing.Optional[str]:
    provider = get_ci_provider()

    if provider == "github_actions":
        return os.getenv("GITHUB_REPOSITORY")

    if provider == "circleci":
        repository_url = os.getenv("CIRCLE_REPOSITORY_URL")
        if repository_url and (
            match := re.match(
                r"(https?://[\w.-]+/)?(?P<full_name>[\w.-]+/[\w.-]+)/?$",
                repository_url,
            )
        ):
            return match.group("full_name")

    if provider == "pytest_mergify_suite":
        return "Mergifyio/pytest-mergify"

    return None


def strtobool(string: str) -> bool:
    if string.lower() in {"y", "yes", "t", "true", "on", "1"}:
        return True

    if string.lower() in {"n", "no", "f", "false", "off", "0"}:
        return False

    raise ValueError(f"Could not convert '{string}' to boolean")
