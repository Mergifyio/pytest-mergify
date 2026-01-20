import json
import os
import pathlib
import typing

from opentelemetry.sdk.resources import Resource, ResourceDetector
from opentelemetry.semconv._incubating.attributes import cicd_attributes, vcs_attributes

from pytest_mergify import utils


def _get_github_workflow_job_head_sha() -> typing.Optional[str]:
    if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
        # NOTE: for pull request, the job head sha is the pull request head_sha,
        # not the GITHUB_SHA, which contains the pull request merge_commit_sha
        event_raw_path = os.getenv("GITHUB_EVENT_PATH")
        if event_raw_path and ((event_path := pathlib.Path(event_raw_path)).is_file()):
            event = json.loads(event_path.read_bytes())
            return str(event["pull_request"]["head"]["sha"])
    return os.getenv("GITHUB_SHA")


def _get_repository_url() -> typing.Optional[str]:
    if "GITHUB_SERVER_URL" in os.environ and "GITHUB_REPOSITORY" in os.environ:
        return os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"]
    return None


def _get_head_ref_name() -> typing.Optional[str]:
    # `GITHUB_HEAD_REF` contains the actual branch name for PRs, while
    # `GITHUB_REF_NAME` contains `<pr_number>/merge`. However, `GITHUB_HEAD_REF`
    # is only set for PR events, so we fall back to `GITHUB_REF_NAME`.
    return os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_REF_NAME")


class GitHubActionsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    OPENTELEMETRY_GHA_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "GITHUB_WORKFLOW"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (str, "GITHUB_JOB"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (int, "GITHUB_RUN_ID"),
        "cicd.pipeline.run.attempt": (int, "GITHUB_RUN_ATTEMPT"),
        "cicd.pipeline.runner.name": (str, "RUNNER_NAME"),
        vcs_attributes.VCS_REF_HEAD_NAME: (str, _get_head_ref_name),
        vcs_attributes.VCS_REF_HEAD_TYPE: (str, "GITHUB_REF_TYPE"),
        vcs_attributes.VCS_REF_BASE_NAME: (str, "GITHUB_BASE_REF"),
        "vcs.repository.name": (str, "GITHUB_REPOSITORY"),
        "vcs.repository.id": (int, "GITHUB_REPOSITORY_ID"),
        vcs_attributes.VCS_REPOSITORY_URL_FULL: (str, _get_repository_url),
        vcs_attributes.VCS_REF_HEAD_REVISION: (str, _get_github_workflow_job_head_sha),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "github_actions":
            return Resource({})

        attributes = utils.get_attributes(self.OPENTELEMETRY_GHA_MAPPING)
        return Resource(attributes)
