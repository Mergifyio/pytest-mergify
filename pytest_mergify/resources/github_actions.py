import os
import pathlib
import json
import typing

from opentelemetry.sdk.resources import Resource, ResourceDetector

from opentelemetry.semconv._incubating.attributes import cicd_attributes
from opentelemetry.semconv._incubating.attributes import vcs_attributes

from pytest_mergify import utils

import requests


class Job(typing.TypedDict):
    id: int
    runner_id: int
    runner_name: str
    runner_group_id: int
    status: str


class GitHubActionsResourceDetector(ResourceDetector):
    """Detects OpenTelemetry Resource attributes for GitHub Actions."""

    @staticmethod
    def get_github_actions_head_sha() -> typing.Optional[str]:
        if os.getenv("GITHUB_EVENT_NAME") == "pull_request":
            # NOTE: we want the head sha of the pull request
            event_raw_path = os.getenv("GITHUB_EVENT_PATH")
            if event_raw_path and (
                (event_path := pathlib.Path(event_raw_path)).is_file()
            ):
                event = json.loads(event_path.read_bytes())
                return str(event["pull_request"]["head"]["sha"])
        return os.getenv("GITHUB_SHA")

    @staticmethod
    def get_github_head_ref_name() -> typing.Optional[str]:
        return os.getenv("GITHUB_HEAD_REF") or os.getenv("GITHUB_REF")

    @staticmethod
    def list_github_jobs() -> typing.Iterable["Job"]:
        try:
            run_id = os.environ["GITHUB_RUN_ID"]
            run_attempts = os.environ["GITHUB_RUN_ATTEMPT"]
            repository_id = os.environ["GITHUB_REPOSITORY_ID"]
            github_server_url = os.environ["GITHUB_SERVER_URL"]
            github_token = os.environ["GITHUB_TOKEN"]
        except KeyError:
            return

        if github_server_url == "https://github.com":
            github_api_url = "https://api.github.com"
        else:
            github_api_url = f"{github_server_url}/api/v3"

        url = f"{github_api_url}/repositories/${repository_id}/actions/runs/{run_id}/attempts/{run_attempts}/jobs?per_page=100"

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
        }
        with requests.Session(headers=headers) as session:
            while url:
                response = session.get(url)
                if response.ok:
                    yield from response.json()["jobs"]
                url = response.links.get("next")

    def match_current_job(
        job: "Job",
        runner_id: int,
        runner_name: str,
    ) -> bool:
        # NOTE(sileht): Only one job can run at a time on a GitHub Action runner
        # So, for a workflow run attempts, we can match the only in_progress job that
        # is running on the runner we are on.

        if job["status"] != "in_progress":
            return False

        # self-hosted/github-hosted ID can clash, so first check the infra
        running_on_github_hosted_runner = runner_name.startswith("GitHub-Actions-")
        job_ran_on_github_hosted_runner = (
            job["runner_group_id"] == 0 and job["runner_name"] == "GitHub Actions"
        )

        if running_on_github_hosted_runner != job_ran_on_github_hosted_runner:
            return False

        return job["runner_id"] == runner_id

    @classmethod
    def get_github_job(cls) -> typing.Optional["Job"]:
        try:
            runner_name = os.environ["RUNNER_NAME"]
            runner_id = os.environ["RUNNER_ID"]
        except KeyError:
            return None

        jobs = [
            job
            for job in cls.list_github_jobs()
            if cls.match_current_job(job, runner_id, runner_name)
        ]
        jobs_count = len(jobs)
        if jobs_count == 1:
            return jobs[0]

        # NOTE(sileht): We should do something to be able to debug this,
        # especialy when job_count > 1

        return None

    OPENTELEMETRY_GHA_MAPPING = {
        cicd_attributes.CICD_PIPELINE_NAME: (str, "GITHUB_JOB"),
        cicd_attributes.CICD_PIPELINE_RUN_ID: (int, "GITHUB_RUN_ID"),
        "cicd.pipeline.run.attempt": (int, "GITHUB_RUN_ATTEMPT"),
        cicd_attributes.CICD_PIPELINE_TASK_NAME: (str, "GITHUB_ACTION"),
        vcs_attributes.VCS_REF_HEAD_TYPE: (str, "GITHUB_REF_TYPE"),
        vcs_attributes.VCS_REF_BASE_NAME: (str, "GITHUB_BASE_REF"),
        "vcs.repository.name": (str, "GITHUB_REPOSITORY"),
        "vcs.repository.id": (int, "GITHUB_REPOSITORY_ID"),
    }

    def detect(self) -> Resource:
        if utils.get_ci_provider() != "github_actions":
            return Resource({})

        attributes = {}

        if "GITHUB_SERVER_URL" in os.environ and "GITHUB_REPOSITORY" in os.environ:
            attributes[vcs_attributes.VCS_REPOSITORY_URL_FULL] = (
                os.environ["GITHUB_SERVER_URL"] + "/" + os.environ["GITHUB_REPOSITORY"]
            )

        head_ref = self.get_github_head_ref_name()
        if head_ref is not None:
            attributes[vcs_attributes.VCS_REF_HEAD_NAME] = head_ref

        head_sha = self.get_github_actions_head_sha()
        if head_sha is not None:
            attributes[vcs_attributes.VCS_REF_HEAD_REVISION] = head_sha

        for attribute_name, (type_, envvar) in self.OPENTELEMETRY_GHA_MAPPING.items():
            if envvar in os.environ:
                attributes[attribute_name] = type_(os.environ[envvar])

        # We do that last to override CICD_PIPELINE_NAME if possible
        if "PYTEST_MERGIFY_GITHUB_TOKEN" in os.environ:
            job = self.get_github_job()
            if job is not None:
                attributes[cicd_attributes.CICD_PIPELINE_NAME] = job.name
                attributes[cicd_attributes.CICD_PIPELINE_ID] = job.id

        return Resource(attributes)
