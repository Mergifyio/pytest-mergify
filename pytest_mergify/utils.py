import os
import typing

CIProviderT = typing.Literal["github_actions", "circleci", "pytest_mergify_suite"]

SUPPORTED_CIs: dict[str, CIProviderT] = {
    "GITHUB_ACTIONS": "github_actions",
    "CIRCLECI": "circleci",
    "_PYTEST_MERGIFY_TEST": "pytest_mergify_suite",
}


def get_ci_provider() -> CIProviderT | None:
    for envvar, name in SUPPORTED_CIs.items():
        if envvar in os.environ and strtobool(os.environ[envvar]):
            return name

    return None


def strtobool(string: str) -> bool:
    if string.lower() in {"y", "yes", "t", "true", "on", "1"}:
        return True

    if string.lower() in {"n", "no", "f", "false", "off", "0"}:
        return False

    raise ValueError(f"Could not convert '{string}' to boolean")
