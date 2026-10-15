"""Criterion 2 with the real server recipe: ``tiled serve catalog`` as a subprocess, over HTTP, with an API key.

The in-process app is faster for everything else, but the command operators actually run
(``info/outputs/parquet/tiled.md``) must be exercised at least once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pyarrow.parquet as pq
import pytest
from tiled.queries import Key

from splash_timepix.parquet_meta import TILED_MARKER_FILENAME
from splash_timepix.tiled_register import cli
from tests.port_utils import get_free_port

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Tiled refuses keys with non-alphanumeric characters (they break ?api_key=... URLs).
API_KEY = "testsecretkey0123456789abcdef"


@pytest.fixture
def tiled_server(tmp_path):
    port = get_free_port()
    catalog = tmp_path / "catalog.db"
    cmd = [
        sys.executable,
        "-m",
        "tiled",
        "serve",
        "catalog",
        "--init",
        "--api-key",
        API_KEY,
        "--read",
        str(tmp_path / "pq"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        str(catalog),
    ]
    (tmp_path / "pq").mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("TILED_API_KEY", None)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    uri = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline and proc.poll() is None:
        try:
            if httpx.get(f"{uri}/api/v1/", timeout=1.0).status_code < 500:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    if not ready:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.kill()
        pytest.fail(f"tiled serve catalog did not come up on {uri}:\n{err[-2000:]}")
    yield uri, catalog
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_recipe_register_over_http_and_read_back(tiled_server, write_scan, monkeypatch, capsys):
    uri, _catalog = tiled_server
    scan_dir = write_scan("scan-1", n_batches=8)

    # Wrong key → cannot register, no marker.
    monkeypatch.setenv("TILED_API_KEY", "wrong")
    assert cli([str(scan_dir), "--uri", uri]) == 1
    assert not (scan_dir / TILED_MARKER_FILENAME).exists()

    # Right key from the environment, as the systemd recipe sets it.
    monkeypatch.setenv("TILED_API_KEY", API_KEY)
    assert cli([str(scan_dir), "--uri", uri, "--path", "tpx3"]) == 0, capsys.readouterr().err
    assert (scan_dir / TILED_MARKER_FILENAME).exists()

    from tiled.client import from_uri

    client = from_uri(uri, api_key=API_KEY)
    node = client["tpx3"]["scan-1"]
    assert list(client["tpx3"].search(Key("user.sample") == "Cu foil")) == ["scan-1"]
    remote = node["events"].read()
    local = pq.read_table(scan_dir / "events").to_pandas()
    assert len(remote) == len(local)
    assert remote.dtypes.to_dict() == local.dtypes.to_dict()
    assert node["events"].structure().npartitions > 1
