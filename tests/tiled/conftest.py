"""Fixtures for the Tiled tests: an in-process Tiled server on a SQLite catalog, and a written scan.

The heavy imports live here so they are only attempted for ``tests/tiled``; with the
``tiled-test`` extra missing the whole folder skips.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

import pytest

tiled_server = pytest.importorskip("tiled.server.app", reason="install splash_timepix[tiled-test]")
pytest.importorskip("dask.dataframe", reason="install splash_timepix[tiled-test]")

from tiled.catalog import from_uri as catalog_from_uri  # noqa: E402
from tiled.client import Context, from_context  # noqa: E402
from tiled.server.app import build_app  # noqa: E402

from splash_timepix.parquet_sink import ParquetSink  # noqa: E402
from tests.parquet.conftest import make_batch, make_random_batch  # noqa: E402


class TiledRig:
    """One in-process Tiled server. ``restart()`` rebuilds the app on the same catalog file."""

    def __init__(self, catalog_db: Path, readable: list[Path], writable: Path):
        self.catalog_db = catalog_db
        self.readable = [str(p) for p in readable]
        self.writable = str(writable)
        self.ctx = None
        self.client = None
        self.start()

    def start(self) -> None:
        tree = catalog_from_uri(
            f"sqlite:///{self.catalog_db}",
            writable_storage=self.writable,
            readable_storage=self.readable,
            init_if_not_exists=True,
        )
        self.ctx = Context.from_app(build_app(tree))
        self.client = from_context(self.ctx)

    def close(self) -> None:
        if self.ctx is not None:
            self.ctx.close()
            self.ctx = None
            self.client = None

    def restart(self) -> None:
        self.close()
        self.start()


@pytest.fixture
def tiled_rig(tmp_path: Path) -> Iterator[Callable[..., TiledRig]]:
    """Factory: ``tiled_rig(readable=[dir, ...])`` → :class:`TiledRig`; all closed at teardown."""
    rigs: list[TiledRig] = []
    counter = {"n": 0}

    def _factory(readable: list[Path]) -> TiledRig:
        counter["n"] += 1
        base = tmp_path / f"tiled{counter['n']}"
        base.mkdir()
        (base / "w").mkdir()
        rig = TiledRig(base / "catalog.db", readable, base / "w")
        rigs.append(rig)
        return rig

    yield _factory
    for rig in rigs:
        rig.close()


@pytest.fixture
def scan_meta() -> dict:
    return dict(
        mode="timing",
        tdc_frequency_hz=1000.0,
        tdc_channel=1,
        tdc_edge="rising",
        t_delta_ns=100.0,
        t_cycle_ns=1e6,
        n_bins=10_000,
        collapse_y=True,
        detector_size_x=256,
        detector_size_y=256,
        source={"kind": "simulator"},
        user={"sample": "Cu foil", "proposal": "P-1"},
        detector={"serval_version": "3.3.2", "chip_ids": ["W0028_H07"]},
    )


@pytest.fixture
def write_scan(tmp_path: Path, scan_meta: dict) -> Callable[..., Path]:
    """``write_scan(name, n_batches=6, roll_bytes=20_000)`` → scan directory under ``tmp_path/pq``.

    Random (poorly compressible) pixel batches so ``events`` rolls into several files at
    this roll size; one well-behaved ``make_batch`` adds TDC and control rows.
    """

    def _write(name: str = "scan-1", n_batches: int = 6, roll_bytes: int = 20_000, incomplete: bool = False) -> Path:
        sink = ParquetSink(tmp_path / "pq", roll_bytes=roll_bytes, row_group_rows=2_000)
        scan_dir = sink.start(name, scan_meta)
        sink.offer(make_batch(2_000, 0, tdc_every=500, control_every=1000))
        for k in range(n_batches):
            sink.offer(make_random_batch(5_000, t0=(k + 1) * 10_000_000, seed=k))
        sink.stop({"total_packets": 42, "total_cycles": 7, "scan_name": name})
        sink.shutdown()
        if incomplete:
            (scan_dir / "_scan_meta.json").unlink()
        return scan_dir

    return _write
