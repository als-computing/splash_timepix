"""
Initialize, Load DACS, Configure DAQ, DAQ (wait until finished)

With ``--meta-json PATH`` the script also snapshots the detector state from Serval
(``/dashboard``, ``/detector/config``, ``/detector/chips``) *after* the configuration
has been applied and *before* the acquisition starts, and merges it into ``PATH`` as
the ``detector`` object. ``tpx-stream --parquet-meta-json PATH`` embeds that object in
every parquet footer, which is how the detector identity (type, chip IDs, Serval
version, bias, thresholds) reaches the files and, from there, Tiled.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from splash_timepix.atomic_io import merge_json_file

from .lib import ServalClient, ServalError

logger = logging.getLogger(__name__)

# Keys of /detector/config that analysts are most likely to filter on. They are
# lifted to the top of the ``detector`` object; the whole config stays under
# ``config`` so nothing is lost if this list turns out to be wrong.
_LIFTED_CONFIG_KEYS = {
    "bias_voltage": ("BiasVoltage",),
    "bias_enabled": ("BiasEnabled",),
    "thresholds": ("Thresholds", "Threshold"),
    "polarity": ("Polarity",),
    "trigger_mode": ("TriggerMode",),
    "exposure_time_s": ("ExposureTime",),
    "trigger_period_s": ("TriggerPeriod",),
    "n_triggers": ("nTriggers",),
    "global_timestamp_interval_s": ("GlobalTimestampInterval",),
}


def _first(d: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def detector_snapshot(client: ServalClient) -> Dict[str, Any]:
    """Return the ``detector`` object: lifted fields plus the raw Serval responses.

    Each GET is attempted independently; a failing one leaves its part empty and
    is recorded under ``errors`` so the acquisition is never blocked by metadata.
    """
    snap: Dict[str, Any] = {"snapshot_time_iso": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    errors: List[str] = []

    dashboard: Dict[str, Any] = {}
    try:
        dashboard = client.get_dashboard() or {}
    except Exception as e:  # ServalError, requests exceptions, JSON errors
        errors.append(f"dashboard: {e}")
    config: Dict[str, Any] = {}
    try:
        config = client.get_detector_config() or {}
    except Exception as e:
        errors.append(f"detector/config: {e}")
    chips: Any = None
    try:
        chips = client.get_detector_chips()
    except Exception as e:
        errors.append(f"detector/chips: {e}")

    server = dashboard.get("Server", {}) if isinstance(dashboard, dict) else {}
    detector = dashboard.get("Detector", {}) if isinstance(dashboard, dict) else {}
    detector_info = detector.get("DetectorInfo", {}) if isinstance(detector, dict) else {}

    snap["serval_version"] = server.get("SoftwareVersion")
    snap["detector_type"] = detector_info.get("DetectorType") or detector_info.get("IfaceName")
    snap["chip_ids"] = _chip_ids(detector_info, chips)
    snap["chip_count"] = detector_info.get("NumberOfChips")
    snap["chip_type"] = detector_info.get("ChipType")
    snap["pixel_count"] = detector_info.get("PixCount")
    snap["chip_temperatures"] = _chip_temperatures(dashboard)
    for key, aliases in _LIFTED_CONFIG_KEYS.items():
        snap[key] = _first(config, aliases)

    snap["config"] = config
    snap["dashboard"] = dashboard
    snap["chips"] = chips
    if errors:
        snap["errors"] = errors
    return snap


def _chip_ids(detector_info: Dict[str, Any], chips: Any) -> Optional[List[Any]]:
    ids = detector_info.get("ChipboardId") or detector_info.get("ChipIds")
    if ids:
        return ids if isinstance(ids, list) else [ids]
    if isinstance(chips, list):
        out = []
        for c in chips:
            if isinstance(c, dict):
                cid = c.get("Id") or c.get("ChipId") or c.get("id")
                if cid is not None:
                    out.append(cid)
        return out or None
    return None


def _chip_temperatures(dashboard: Any) -> Optional[List[Any]]:
    if not isinstance(dashboard, dict):
        return None
    det = dashboard.get("Detector", {})
    if not isinstance(det, dict):
        return None
    for key in ("ChipTemperatures", "Chiptemps", "ChipTemps"):
        if key in det:
            return det[key]
    info = det.get("DetectorInfo", {})
    if isinstance(info, dict) and "ChipTemperatures" in info:
        return info["ChipTemperatures"]
    return None


def write_detector_snapshot(meta_json: Path, client: ServalClient) -> Dict[str, Any]:
    """Snapshot the detector and merge it into ``meta_json`` as ``detector`` (atomic read-merge-write).

    Never raises: on any failure an empty ``detector`` object is written (if even
    that fails, the error is logged) and the caller proceeds with the acquisition.
    """
    try:
        snapshot = detector_snapshot(client)
    except Exception as e:  # pragma: no cover - detector_snapshot already guards each GET
        logger.warning("Detector snapshot failed: %s", e)
        snapshot = {"errors": [str(e)]}
    try:
        merge_json_file(meta_json, {"detector": snapshot})
        if snapshot.get("errors"):
            logger.warning("Detector snapshot incomplete: %s", "; ".join(snapshot["errors"]))
        else:
            logger.info("Detector snapshot written to %s", meta_json)
    except OSError as e:
        logger.warning("Could not write detector snapshot to %s: %s", meta_json, e)
    return snapshot


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TimePix3 data acquisition")
    parser.add_argument(
        "-time",
        type=int,
        default=19008000,  # longest possible acquisition 220 days
        help="Acquisition duration in seconds (default: inf)",
    )
    parser.add_argument(
        "-output",
        type=str,
        default="/home/tpx/Desktop/tpxLOCAL/data",
        help="Output directory for data files (default: /home/tpx/Desktop/tpx3LOCAL/data)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: stream only, no file writing",
    )
    parser.add_argument(
        "--meta-json",
        type=str,
        default=None,
        help="Merge a snapshot of the detector state (Serval dashboard/config/chips) into this JSON "
        "file as 'detector' just before the acquisition starts; tpx-stream embeds it in the parquet footers",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    # Data and settings locations
    OUTPUT_DIR = Path(args.output)
    BPC_FILE = Path("/home/tpx/Desktop/tpx3LOCAL/Factory-Settings/pix-config.bpc")
    DACS_FILE = Path("/home/tpx/Desktop/tpx3LOCAL/Factory-Settings/pix-config.bpc.dacs")
    # Configuration variables
    TRIGGER_MODE = "CONTINUOUS"
    N_TRIGGERS = args.time  # acquisition duration (in seconds) for continuous mode
    TRIGGER_PERIOD = 1.0  # leave this at 1 second for continuous mode
    EXPOSURE_TIME = 1.0  # equal to trigger period for continuous mode

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    client = ServalClient()  # use default BASE_URL
    client.check_connection()
    logging.debug("Connected to Serval")

    client.wait_for_detector()
    logging.debug("Camera connected to Serval")

    # Dashboard info
    dashboard = client.get_dashboard()
    logging.info(f"Server Software Version: {dashboard.get('Server', {}).get('SoftwareVersion')}")

    # Initialize camera
    client.load_configuration("pixelconfig", BPC_FILE)
    client.load_configuration("dacs", DACS_FILE)

    # Detector configuration
    det_cfg = client.get_detector_config()
    det_cfg.update(
        {
            "nTriggers": N_TRIGGERS,
            "TriggerMode": TRIGGER_MODE,
            "TriggerPeriod": TRIGGER_PERIOD,
            "ExposureTime": EXPOSURE_TIME,
        }
    )
    client.update_detector_config(det_cfg)

    # Detector snapshot for the parquet footers: now is the first moment the values are
    # the ones this scan will actually run with (pixel config, DACs and trigger settings
    # have all been applied), and data cannot flow before start_acquisition below.
    if args.meta_json:
        write_detector_snapshot(Path(args.meta_json), client)

    # Set data destination based on mode
    if args.preview:
        # Preview mode: streaming only, no file writing
        logging.info("PREVIEW MODE: Streaming only, no file saving")
        destination = {
            "Raw": [{"Base": "tcp://connect@localhost:7070", "QueueSize": 16384}],
        }
    else:
        # Full acquisition mode: streaming + file writing
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        destination = {
            "Raw": [
                {"Base": "tcp://connect@localhost:7070", "QueueSize": 16384},
                {"Base": Path(OUTPUT_DIR).as_uri(), "FilePattern": ""},
            ],
        }

    client.set_destination(destination)

    mode_str = "preview" if args.preview else "acquisition"
    logging.info(f"Starting {mode_str} for {N_TRIGGERS} seconds.")
    if not args.preview:
        logging.info(f"Output directory: {OUTPUT_DIR}")
    start_time = time.time()

    # Start and wait for acquisition
    time.sleep(1)
    client.start_acquisition()
    client.wait_for_measurement_to_finish()

    end_time = time.time()
    duration = end_time - start_time
    logging.info(f"Acquisition took {duration:.2f} seconds.")


__all__ = ["ServalClient", "ServalError", "detector_snapshot", "write_detector_snapshot", "main", "parse_args"]


if __name__ == "__main__":
    main(sys.argv[1:])
