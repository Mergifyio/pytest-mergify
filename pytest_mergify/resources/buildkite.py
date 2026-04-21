import os

from opentelemetry.sdk.resources import Resource, ResourceDetector
from opentelemetry.semconv._incubating.attributes import cicd_attributes, vcs_attributes

from pytest_mergify import utils
from pytest_mergify.resources import git


class BuildkiteResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for Buildkite."""

    OPENTELEMETRY_BUILDKITE_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "BUILDKITE_PIPELINE_SLUG"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (
            str,
            lambda: os.getenv("BUILDKITE_LABEL") or os.getenv("BUILDKITE_STEP_KEY"),
        ),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (str, "BUILDKITE_BUILD_ID"),
        "cicd.pipeline.run.url": (str, "BUILDKITE_BUILD_URL"),
        "cicd.pipeline.run.attempt": (
            int,
            lambda: int(os.getenv("BUILDKITE_RETRY_COUNT", "0")) + 1,
        ),
        "cicd.pipeline.runner.name": (str, "BUILDKITE_AGENT_NAME"),
        vcs_attributes.VCS_REF_HEAD_NAME: (str, "BUILDKITE_BRANCH"),
        vcs_attributes.VCS_REF_BASE_NAME: (str, "BUILDKITE_PULL_REQUEST_BASE_BRANCH"),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, "BUILDKITE_COMMIT"),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, "BUILDKITE_REPO"),
        "vcs.repository.name": (
            str,
            lambda: utils.get_repository_name_from_env_url("BUILDKITE_REPO"),
        ),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "buildkite":
            return Resource({})

        attributes = utils.get_attributes(
            git.GitResourceDetector.OPENTELEMETRY_GIT_MAPPING
        )
        attributes.update(utils.get_attributes(self.OPENTELEMETRY_BUILDKITE_MAPPING))

        return Resource(attributes)
