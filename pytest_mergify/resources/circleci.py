from opentelemetry.sdk.resources import Resource, ResourceDetector
from opentelemetry.semconv._incubating.attributes import cicd_attributes, vcs_attributes

from pytest_mergify import utils


class CircleCIResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for CircleCI."""

    OPENTELEMETRY_CIRCLECI_MAPPING = {
        # CircleCI publishes no workflow name: `CIRCLE_WORKFLOW_ID` is a UUID
        # that differs on every run, so it cannot identify the same pipeline
        # across runs. The job name stands in for both, as the Jenkins detector
        # already does with `JOB_NAME`. Job names are unique within a project's
        # config, so the pair still identifies one job run after run.
        cicd_attributes.CICD_PIPELINE_NAME: (str, "CIRCLE_JOB"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (str, "CIRCLE_JOB"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (str, "CIRCLE_WORKFLOW_ID"),
        "cicd.pipeline.run.url": (str, "CIRCLE_BUILD_URL"),
        vcs_attributes.VCS_REF_HEAD_NAME: (str, "CIRCLE_BRANCH"),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, "CIRCLE_SHA1"),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, "CIRCLE_REPOSITORY_URL"),
        "vcs.repository.name": (
            str,
            lambda: utils.get_repository_name_from_env_url("CIRCLE_REPOSITORY_URL"),
        ),
        # No base branch is published either, so a pull request run is not
        # distinguishable from a branch run here. Flaky detection reads that
        # absence as a push run and stays in `unhealthy` mode.
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "circleci":
            return Resource({})

        return Resource(utils.get_attributes(self.OPENTELEMETRY_CIRCLECI_MAPPING))
