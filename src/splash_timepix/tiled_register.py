"""Register a parquet scan directory in a Tiled catalog — ``tpx-tiled-register``.

This is criterion 3 of the parquet plan taken literally: the metadata Tiled stores
comes **from the parquet files** (their ``splash_timepix`` footer blocks and
row-group statistics), not from a sidecar. ``_scan_meta.json`` is only the
completion marker and a cross-check. The files stay where ``tpx-stream`` wrote
them; Tiled serves them in place through its built-in parquet adapter.

Tree created under ``--path`` (default ``tpx3``)::

    <path>/<key>/            container, spec tpx3_scan, full scan metadata (what search returns)
        events               table, spec tpx3_events, one partition per file
        tdc                  table, spec tpx3_tdc
        control              table, spec tpx3_control

Re-running on the same scan is idempotent: an unchanged file set refreshes the
metadata and keeps the node (so what people added in Tiled survives); a changed
file set — or ``--force`` — deletes and recreates it, because data sources cannot
be updated in place. Success writes ``_tiled_registered.json`` into the scan
directory; ``--unregistered`` walks a root and registers every scan without it.

The API key comes from ``TILED_API_KEY`` or ``--api-key``; it is never read from
or written to the UI preferences.
"""

from __future__ import annotations

import getpass
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pyarrow.parquet as pq
import typer

from splash_timepix.atomic_io import atomic_write_json, read_json
from splash_timepix.parquet_meta import (
    SCAN_META_FILENAME,
    TABLE_SCHEMAS,
    TABLES,
    TILED_MARKER_FILENAME,
    ScanFields,
    ScanFormatError,
    ScanSummary,
    list_part_files,
    package_version,
    read_file_metadata,
    read_scan_summary,
    ticks_to_seconds,
)

logger = logging.getLogger(__name__)

PARQUET_MIMETYPE = "application/x-parquet"
SCAN_SPEC = "tpx3_scan"
DEFAULT_URI = "http://localhost:8000"
DEFAULT_PATH = "tpx3"


class RegistrationError(RuntimeError):
    """Registration refused or failed; the message says why."""


class IncompleteScanError(RegistrationError):
    """The scan has no ``_scan_meta.json`` and ``allow_incomplete`` was not given."""


# ---------------------------------------------------------------------------
# Step 1: metadata from the files
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_facts(path: Path) -> Dict[str, Any]:
    """Row count, row groups, size and ``t`` statistics of one file, from its footer only."""
    md = pq.read_metadata(path)
    t_min: Optional[int] = None
    t_max: Optional[int] = None
    try:
        t_idx = md.schema.names.index("t")
    except ValueError:
        t_idx = -1
    if t_idx >= 0:
        for rg in range(md.num_row_groups):
            col = md.row_group(rg).column(t_idx)
            st = col.statistics
            if st is None or not st.has_min_max:
                continue
            t_min = int(st.min) if t_min is None else min(t_min, int(st.min))
            t_max = int(st.max) if t_max is None else max(t_max, int(st.max))
    return {
        "name": path.name,
        "num_rows": md.num_rows,
        "num_row_groups": md.num_row_groups,
        "bytes": path.stat().st_size,
        "t_first": t_min,
        "t_last": t_max,
    }


def files_signature(files_by_table: Dict[str, List[Path]]) -> List[List[Any]]:
    """What identifies a registration's file set: (table, name, size) sorted."""
    sig = []
    for table in TABLES:
        for p in files_by_table.get(table, []):
            sig.append([table, p.name, p.stat().st_size])
    return sorted(sig)


def collect_scan(scan_dir: Path, allow_incomplete: bool = False) -> Tuple[Dict[str, Any], Dict[str, List[Path]]]:
    """Build the node metadata for ``scan_dir`` from its parquet footers.

    Returns ``(metadata, files_by_table)``. Raises :class:`IncompleteScanError`
    when the sidecar is missing and ``allow_incomplete`` is False, and
    :class:`RegistrationError` when the directory holds no readable scan.
    """
    scan_dir = Path(scan_dir)
    if not scan_dir.is_dir():
        raise RegistrationError(f"{scan_dir} is not a directory")
    sidecar = scan_dir / SCAN_META_FILENAME
    if not sidecar.exists() and not allow_incomplete:
        raise IncompleteScanError(
            f"{scan_dir}: no {SCAN_META_FILENAME} — the scan did not finish (or is still running). "
            "Pass --allow-incomplete to register the footer-complete files anyway."
        )

    files_by_table: Dict[str, List[Path]] = {t: list_part_files(scan_dir / t) for t in TABLES}
    if not any(files_by_table.values()):
        raise RegistrationError(f"{scan_dir}: no completed parquet files")

    # Per-scan fields: identical in every footer; take them from the first file we find,
    # and check the others agree on scan_name (a mixed directory would be a real problem).
    scan_fields: Optional[Dict[str, Any]] = None
    tables: Dict[str, Dict[str, Any]] = {}
    first_batch_wall: Optional[str] = None
    dropped_rows = 0
    monotonic_all = True
    for table in TABLES:
        facts = []
        for p in files_by_table[table]:
            meta = read_file_metadata(p)
            if scan_fields is None:
                scan_fields = ScanFields.model_validate(meta.model_dump()).model_dump(mode="json")
            elif meta.scan_name != scan_fields["scan_name"]:
                raise RegistrationError(
                    f"{p}: footer scan_name {meta.scan_name} differs from {scan_fields['scan_name']}"
                )
            f = _file_facts(p)
            f["t_monotonic"] = meta.t_monotonic
            if meta.t_monotonic is False:
                monotonic_all = False
            facts.append(f)
            dropped_rows = max(dropped_rows, meta.dropped_rows_so_far)
            first_batch_wall = first_batch_wall or meta.first_batch_wall_time_iso
        t_firsts = [f["t_first"] for f in facts if f["t_first"] is not None]
        t_lasts = [f["t_last"] for f in facts if f["t_last"] is not None]
        tables[table] = {
            "num_rows": sum(f["num_rows"] for f in facts),
            "num_files": len(facts),
            "num_row_groups": sum(f["num_row_groups"] for f in facts),
            "bytes": sum(f["bytes"] for f in facts),
            "files": [f["name"] for f in facts],
            "t_first": min(t_firsts) if t_firsts else None,
            "t_last": max(t_lasts) if t_lasts else None,
        }
    assert scan_fields is not None  # guaranteed by the any(files) check above
    clock = int(scan_fields.get("clock_hz") or 0)
    for tbl in tables.values():
        tbl["t_first_s"] = ticks_to_seconds(tbl["t_first"], clock) if (tbl["t_first"] is not None and clock) else None
        tbl["t_last_s"] = ticks_to_seconds(tbl["t_last"], clock) if (tbl["t_last"] is not None and clock) else None

    metadata: Dict[str, Any] = dict(scan_fields)
    metadata["tables"] = tables
    ev = tables["events"]
    # Top-level copies of the events time range so a range search is one key away.
    metadata["t_first"] = ev["t_first"]
    metadata["t_last"] = ev["t_last"]
    metadata["t_first_s"] = ev["t_first_s"]
    metadata["t_last_s"] = ev["t_last_s"]
    metadata["num_events"] = ev["num_rows"]
    metadata["t_monotonic"] = monotonic_all
    metadata["first_batch_wall_time_iso"] = first_batch_wall
    metadata["dropped_rows"] = dropped_rows

    # Sidecar: completion marker and cross-check only.
    if sidecar.exists():
        summary = read_scan_summary(scan_dir)
        metadata["status"] = summary.status
        metadata["stop_time_iso"] = summary.stop_time_iso
        metadata["acquisition_duration_s"] = summary.acquisition_duration_s
        metadata["dropped_rows"] = summary.dropped_rows
        metadata["dropped_batches"] = summary.dropped_batches
        metadata["stop_message"] = summary.stop_message
        if summary.error:
            metadata["error"] = summary.error
        _cross_check(scan_dir, summary, tables)
    else:
        metadata["status"] = "incomplete"
        metadata["stop_message"] = None

    ui = find_ui_metadata(scan_dir, scan_fields["scan_name"])
    if ui:
        metadata["ui"] = ui
    return metadata, files_by_table


def _cross_check(scan_dir: Path, summary: ScanSummary, tables: Dict[str, Dict[str, Any]]) -> None:
    for table, tbl in tables.items():
        side = summary.tables.get(table)
        if side is None:
            continue
        if side.num_rows != tbl["num_rows"] or side.num_files != tbl["num_files"]:
            logger.warning(
                "%s: %s says %s has %d rows in %d files, the footers say %d rows in %d files; the footers win",
                scan_dir,
                SCAN_META_FILENAME,
                table,
                side.num_rows,
                side.num_files,
                tbl["num_rows"],
                tbl["num_files"],
            )


def find_ui_metadata(scan_dir: Path, scan_name: str) -> Dict[str, Any]:
    """The UI's ``*_meta.json`` for this scan (written next to the parquet root), as a ``ui`` block.

    Adds ``raw_tpx3_path`` when the slug-renamed ``.tpx3`` sits beside it — the link between
    the raw and the parquet record of the same scan.
    """
    root = Path(scan_dir).parent
    try:
        candidates = sorted(root.glob("*_meta.json"))
    except OSError:
        return {}
    for p in candidates:
        doc = read_json(p, default=None)
        if not isinstance(doc, dict) or doc.get("scan_name") != scan_name:
            continue
        ui = {k: v for k, v in doc.items() if k not in ("scan_name", "stop_message")}
        ui["meta_json_path"] = str(p)
        stem = p.name[: -len("_meta.json")]
        tpx3 = root / f"{stem}.tpx3"
        if tpx3.exists():
            ui["raw_tpx3_path"] = str(tpx3)
        ui["slug"] = stem
        return ui
    return {}


def default_key(scan_dir: Path, metadata: Dict[str, Any]) -> str:
    """Node key: the UI's slug when known (human readable), else the scan uuid."""
    ui = metadata.get("ui") or {}
    return ui.get("slug") or metadata["scan_name"]


# ---------------------------------------------------------------------------
# Step 2: talk to Tiled
# ---------------------------------------------------------------------------


def connect(uri: str = DEFAULT_URI, api_key: Optional[str] = None, path: str = DEFAULT_PATH):
    """Return the Tiled container at ``path`` (``a/b/c``), creating missing levels."""
    from tiled.client import from_uri

    client = from_uri(uri, api_key=api_key)
    return ensure_path(client, path)


def ensure_path(client, path: str):
    node = client
    for part in [p for p in path.strip("/").split("/") if p]:
        if part in node:
            node = node[part]
        else:
            node = node.create_container(key=part)
    return node


def _structure_for(table: str, n_files: int):
    from tiled.structures.table import TableStructure

    return TableStructure.from_schema(TABLE_SCHEMAS[table].remove_metadata(), npartitions=n_files)


def _register_tables(scan_node, scan_dir: Path, files_by_table: Dict[str, List[Path]], metadata: Dict[str, Any]):
    from tiled.structures.core import StructureFamily
    from tiled.structures.data_source import Asset, DataSource, Management

    for table in TABLES:
        files = files_by_table[table]
        if not files:
            continue
        scan_node.new(
            structure_family=StructureFamily.table,
            data_sources=[
                DataSource(
                    management=Management.external,
                    mimetype=PARQUET_MIMETYPE,
                    structure_family=StructureFamily.table,
                    structure=_structure_for(table, len(files)),
                    assets=[
                        Asset(data_uri=f.resolve().as_uri(), is_directory=False, parameter="data_uris", num=i)
                        for i, f in enumerate(files)
                    ],
                )
            ],
            key=table,
            metadata={"table": table, "scan_name": metadata["scan_name"], **metadata["tables"][table]},
            specs=[f"tpx3_{table}"],
        )


def _read_back(scan_node, files_by_table: Dict[str, List[Path]]) -> None:
    """Fail loudly now rather than at the first analysis: readable_storage is enforced at read time."""
    for table in TABLES:
        if not files_by_table[table]:
            continue
        try:
            df = scan_node[table].read_partition(0)
        except Exception as e:  # tiled raises its own error types; message matters more than type
            raise RegistrationError(
                f"registered, but Tiled cannot read {table}: {e}. Is the scan directory under one of the "
                "server's --read (readable_storage) locations?"
            ) from e
        expected = pq.read_metadata(files_by_table[table][0]).num_rows
        if len(df) != expected:
            raise RegistrationError(f"read-back of {table} partition 0 returned {len(df)} rows, file has {expected}")


def register_scan(
    container,
    scan_dir: Path,
    *,
    key: Optional[str] = None,
    allow_incomplete: bool = False,
    force: bool = False,
    uri: str = DEFAULT_URI,
    path: str = DEFAULT_PATH,
) -> Dict[str, Any]:
    """Register one scan directory under ``container``. Returns the marker document."""
    scan_dir = Path(scan_dir)
    metadata, files_by_table = collect_scan(scan_dir, allow_incomplete=allow_incomplete)
    key = key or default_key(scan_dir, metadata)
    signature = files_signature(files_by_table)
    metadata["registration"] = {
        "tool": "tpx-tiled-register",
        "splash_timepix_version": package_version(),
        "registered_by": f"{getpass.getuser()}@{socket.gethostname()}",
        "register_time_iso": _utc_now_iso(),
        "scan_dir": str(scan_dir.resolve()),
        "files_signature": signature,
    }

    action = "created"
    existing = container[key] if key in container else None
    if existing is not None:
        specs = [s.name if hasattr(s, "name") else str(s) for s in (existing.specs or [])]
        if SCAN_SPEC not in specs:
            raise RegistrationError(f"{path}/{key} exists in Tiled but is not a {SCAN_SPEC} node; choose another --key")
        same_scan = existing.metadata.get("scan_name") == metadata["scan_name"]
        same_files = (existing.metadata.get("registration") or {}).get("files_signature") == signature
        if same_scan and same_files and not force:
            # Keep the node (and anything people added to it); refresh our part of the metadata.
            existing.update_metadata(metadata=metadata)
            scan_node = container[key]
            action = "updated"
        else:
            existing.delete(recursive=True, external_only=True)
            existing = None
            action = "recreated"
    if existing is None:
        scan_node = container.create_container(key=key, metadata=metadata, specs=[SCAN_SPEC])
        _register_tables(scan_node, scan_dir, files_by_table, metadata)

    _read_back(scan_node, files_by_table)

    marker = {
        "uri": uri,
        "path": path,
        "key": key,
        "node_uri": getattr(scan_node, "uri", None),
        "scan_name": metadata["scan_name"],
        "status": metadata["status"],
        "action": action,
        "register_time_iso": metadata["registration"]["register_time_iso"],
        "files_signature": signature,
    }
    atomic_write_json(scan_dir / TILED_MARKER_FILENAME, marker)
    logger.info("Registered %s as %s/%s (%s)", scan_dir, path, key, action)
    return marker


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------


def is_scan_dir(path: Path) -> bool:
    p = Path(path)
    return p.is_dir() and ((p / SCAN_META_FILENAME).exists() or bool(list_part_files(p / "events")))


def find_unregistered(scan_root: Path) -> List[Path]:
    """Scan directories under ``scan_root`` without a ``_tiled_registered.json`` marker."""
    root = Path(scan_root)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if is_scan_dir(p) and not (p / TILED_MARKER_FILENAME).exists():
            out.append(p)
    return out


def register_unregistered(
    container, scan_root: Path, *, allow_incomplete: bool = False, uri: str = DEFAULT_URI, path: str = DEFAULT_PATH
) -> Dict[str, List[str]]:
    """Register every unregistered scan under ``scan_root``.

    Returns ``{"registered": [...], "skipped": [...], "failed": [...]}`` (lists of directories).
    """
    result: Dict[str, List[str]] = {"registered": [], "skipped": [], "failed": []}
    for scan_dir in find_unregistered(scan_root):
        try:
            register_scan(container, scan_dir, allow_incomplete=allow_incomplete, uri=uri, path=path)
            result["registered"].append(str(scan_dir))
        except IncompleteScanError as e:
            logger.info("Skipped %s: %s", scan_dir, e)
            result["skipped"].append(str(scan_dir))
        except (RegistrationError, ScanFormatError) as e:
            logger.error("Failed %s: %s", scan_dir, e)
            result["failed"].append(str(scan_dir))
        except Exception as e:  # network, auth … keep going with the next scan
            logger.error("Failed %s: %s: %s", scan_dir, type(e).__name__, e)
            result["failed"].append(str(scan_dir))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    scan_dir: Optional[Path] = typer.Argument(None, help="Scan directory (<parquet_dir>/<scan_uuid>)"),
    uri: str = typer.Option(DEFAULT_URI, help="Tiled server URI"),
    api_key: Optional[str] = typer.Option(None, help="API key (default: TILED_API_KEY environment variable)"),
    path: str = typer.Option(DEFAULT_PATH, help="Container path in Tiled under which scans are registered"),
    key: Optional[str] = typer.Option(None, help="Node key (default: the UI slug if known, else the scan uuid)"),
    allow_incomplete: bool = typer.Option(False, help="Register a scan that has no _scan_meta.json"),
    force: bool = typer.Option(False, help="Delete and recreate an existing node even if the files are unchanged"),
    scan_root: Optional[Path] = typer.Option(None, help="With --unregistered: the parquet root to walk"),
    unregistered: bool = typer.Option(False, help="Register every scan under --scan-root without a marker"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Register a parquet scan (or every unregistered scan under a root) in Tiled."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    api_key = api_key or os.environ.get("TILED_API_KEY")
    if unregistered:
        if scan_root is None:
            typer.echo("--unregistered needs --scan-root", err=True)
            raise typer.Exit(code=2)
    elif scan_dir is None:
        typer.echo("Give a scan directory, or --unregistered --scan-root DIR", err=True)
        raise typer.Exit(code=2)

    try:
        container = connect(uri, api_key=api_key, path=path)
    except Exception as e:
        typer.echo(f"Cannot connect to Tiled at {uri}: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1)

    if unregistered:
        assert scan_root is not None
        result = register_unregistered(container, scan_root, allow_incomplete=allow_incomplete, uri=uri, path=path)
        typer.echo(
            f"registered {len(result['registered'])}, skipped {len(result['skipped'])} incomplete, "
            f"failed {len(result['failed'])}"
        )
        for d in result["failed"]:
            typer.echo(f"  failed: {d}", err=True)
        raise typer.Exit(code=1 if result["failed"] else 0)

    assert scan_dir is not None
    try:
        marker = register_scan(
            container, scan_dir, key=key, allow_incomplete=allow_incomplete, force=force, uri=uri, path=path
        )
    except IncompleteScanError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=2)
    except (RegistrationError, ScanFormatError) as e:
        typer.echo(f"Registration failed: {e}", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"Registration failed: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{marker['action']}: {path}/{marker['key']}  ({marker['status']})  {marker['node_uri'] or ''}")


def cli(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point usable from tests: returns the exit code instead of raising SystemExit."""
    try:
        rv = app(args=list(argv) if argv is not None else None, standalone_mode=False)
    except typer.Exit as e:
        return int(e.exit_code)
    except SystemExit as e:  # pragma: no cover
        return int(e.code or 0)
    # With standalone_mode=False Click returns the exit code of a raised Exit instead of exiting.
    return int(rv) if isinstance(rv, int) else 0


__all__ = [
    "RegistrationError",
    "IncompleteScanError",
    "collect_scan",
    "files_signature",
    "find_ui_metadata",
    "default_key",
    "connect",
    "ensure_path",
    "register_scan",
    "find_unregistered",
    "register_unregistered",
    "app",
    "cli",
]


if __name__ == "__main__":
    sys.exit(cli())
