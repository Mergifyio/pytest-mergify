import os


# Set this before we call any part of our plugin
def pytest_cmdline_main() -> None:
    os.environ["CI"] = "1"
    os.environ["_PYTEST_MERGIFY_TEST"] = "1"
    os.environ["MERGIFY_API_URL"] = "https://localhost/v1/ci/traces"
