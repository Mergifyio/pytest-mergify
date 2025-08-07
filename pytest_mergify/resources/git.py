import typing

from opentelemetry.sdk.resources import Resource, ResourceDetector
from opentelemetry.semconv._incubating.attributes import vcs_attributes

from pytest_mergify import utils


def _get_git_commit() -> typing.Optional[str]:
    return utils.git("rev-parse", "HEAD")


def _get_git_branch() -> typing.Optional[str]:
    return utils.git("rev-parse", "--abbrev-ref", "HEAD")


def _get_git_url() -> typing.Optional[str]:
    return utils.git("config", "--get", "remote.origin.url")


def _get_repository_name() -> typing.Optional[str]:
    repository_url = _get_git_url()
    if repository_url:
        utils.get_repository_name_from_url(repository_url)
    return None


class GitResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    OPENTELEMETRY_GIT_MAPPING = {
        vcs_attributes.VCS_REF_HEAD_NAME: (str, _get_git_branch),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, _get_git_commit),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, _get_git_url),
        "vcs.repository.name": (str, _get_repository_name),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() is None:
            return Resource({})

        attributes = utils.get_attributes(self.OPENTELEMETRY_GIT_MAPPING)
        return Resource(attributes)
