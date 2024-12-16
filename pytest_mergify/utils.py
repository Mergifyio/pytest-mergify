import os
import typing

CIProviderT = typing.Literal["github_actions", "circleci"]


def get_ci_provider() -> CIProviderT | None:
    if os.getenv("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if os.getenv("CIRCLECI") == "true":
        return "circleci"
    return None


def strtobool(string: str) -> bool:
    if string.lower() in {"y", "yes", "t", "true", "on", "1"}:
        return True

    if string.lower() in {"n", "no", "f", "false", "off", "0"}:
        return False

    raise ValueError(f"Could not convert '{string}' to boolean")
