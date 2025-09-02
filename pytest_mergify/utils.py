import os
import re
import subprocess
import typing

CIProviderT = typing.Literal[
    "github_actions", "circleci", "pytest_mergify_suite", "jenkins"
]

SUPPORTED_CIs: typing.Dict[str, CIProviderT] = {
    "GITHUB_ACTIONS": "github_actions",
    "CIRCLECI": "circleci",
    "JENKINS_URL": "jenkins",
    "_PYTEST_MERGIFY_TEST": "pytest_mergify_suite",
}


def is_in_ci() -> bool:
    return strtobool(os.environ.get("CI", "false")) or strtobool(
        os.environ.get("PYTEST_MERGIFY_ENABLE", "false")
    )


def get_ci_provider() -> typing.Optional[CIProviderT]:
    for envvar, name in SUPPORTED_CIs.items():
        if envvar in os.environ:
            try:
                enabled = strtobool(os.environ[envvar])
            except ValueError:
                # Not a boolean, just check it's not empty
                enabled = bool(os.environ[envvar].strip())
            if enabled:
                return name

    return None


def get_repository_name_from_url(repository_url: str) -> typing.Optional[str]:
    # Handle SSH Git URLs like git@github.com:owner/repo.git
    if match := re.match(
        r"git@[\w.-]+:(?P<full_name>[\w.-]+/[\w.-]+)(?:\.git)?/?$",
        repository_url,
    ):
        full_name = match.group("full_name")
        # Remove .git suffix if present
        if full_name.endswith(".git"):
            full_name = full_name[:-4]
        return full_name

    # Handle HTTPS/HTTP URLs like https://github.com/owner/repo (with optional port)
    if match := re.match(
        r"(https?://[\w.-]+(?::\d+)?/)?(?P<full_name>[\w.-]+/[\w.-]+)/?$",
        repository_url,
    ):
        return match.group("full_name")

    return None


def get_repository_name_from_env_url(env: str) -> typing.Optional[str]:
    repository_url = os.getenv(env)
    if repository_url:
        return get_repository_name_from_url(repository_url)

    return None


class InvalidRepositoryFullNameError(Exception):
    pass


def split_full_repo_name(
    full_repo_name: str,
) -> typing.Tuple[str, str]:
    split_name = full_repo_name.split("/")
    if len(split_name) == 2:
        return split_name[0], split_name[1]

    raise InvalidRepositoryFullNameError(f"Invalid repository name: {full_repo_name}")


def get_repository_name() -> typing.Optional[str]:
    provider = get_ci_provider()

    if provider == "jenkins":
        return get_repository_name_from_env_url("GIT_URL")

    if provider == "github_actions":
        return os.getenv("GITHUB_REPOSITORY")

    if provider == "circleci":
        return get_repository_name_from_env_url("CIRCLE_REPOSITORY_URL")

    if provider == "pytest_mergify_suite":
        return "Mergifyio/pytest-mergify"

    repository_url = git("config", "--get", "remote.origin.url")
    if repository_url:
        return get_repository_name_from_url(repository_url)

    return None


def strtobool(string: str) -> bool:
    if string.lower() in {"y", "yes", "t", "true", "on", "1"}:
        return True

    if string.lower() in {"n", "no", "f", "false", "off", "0"}:
        return False

    raise ValueError(f"Could not convert '{string}' to boolean")


# NOTE(sileht): Can't use NewType because python 3.8
def get_attributes(
    mapping: typing.Dict[
        str,
        # NOTE(sileht): does not work on py38
        #   tuple[
        #        type[typing.Union[str, int]],
        #        typing.Union[str, typing.Callable[[], typing.Optional[str]]],
        #    ],
        typing.Any,
    ],
) -> typing.Dict[str, typing.Union[str, int]]:
    attributes = {}
    for attr, (cast, env_or_callable) in mapping.items():
        value: typing.Optional[str]
        if callable(env_or_callable):
            value = env_or_callable()
        else:
            value = os.getenv(env_or_callable)
        if value is not None:
            attributes[attr] = cast(value)
    return attributes


def git(*args: str) -> typing.Optional[str]:
    try:
        return subprocess.check_output(
            ["git", *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None
