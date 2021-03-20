import json
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

PROJECT_PATH = Path(__file__).parent.parent
STATIC_PATH = PROJECT_PATH / "static"


def query(host, query_str, **query_kwargs):
    query = {"query": query_str, **query_kwargs}
    query_data = json.dumps(query).encode()
    with urlopen(host, data=query_data) as page:
        return json.load(page)


def health_check(host):
    try:
        query(host, "Name()")
    except URLError:
        return False
    else:
        return True


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig):
    return PROJECT_PATH / "docker-compose.yml"


@pytest.fixture(scope="session")
def reiz_service():
    process = subprocess.Popen(["docker-compose", "up"])
    while not health_check():
        time.sleep(30)
    yield
    process.terminate()


def test_reiz_web_api_basic(reiz_service):
    assert query("Name()")
