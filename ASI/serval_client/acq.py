"""
Initialize, Load DACS, Configure DAQ, DAQ (wait until finished)
"""
import argparse
import logging
from pathlib import Path
import time
#
from lib import ServalError, ServalClient


def main() -> None:
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="TimePix3 data acquisition")
    parser.add_argument(
        "-time",
        type=int,
        default=19008000, # longest possible acquisition 220 days
        help="Acquisition duration in seconds (default: inf)"
    )
    parser.add_argument(
        "-output",
        type=str,
        default="/home/tpx/Desktop/tpxLOCAL/data",
        #default="/home/tpx/Desktop/deleteLater",
        help="Output directory for data files (default: /home/tpx/Desktop/tpx3LOCAL/data)"
    )
    args = parser.parse_args()

    # Configuration variables
    BASE_URL = "http://localhost:8080" # default: 8080

    BPC_FILE = Path("/home/tpx/Desktop/tpx3LOCAL/Factory-Settings/pix-config.bpc")
    DACS_FILE = Path("/home/tpx/Desktop/tpx3LOCAL/Factory-Settings/pix-config.bpc.dacs")

    TRIGGER_MODE = "CONTINUOUS"
    N_TRIGGERS = args.time  # acquisition duration (in seconds) for continuous mode
    TRIGGER_PERIOD = 1.0  # leave this at 1 second for continuous mode
    EXPOSURE_TIME = 1.0  # equal to trigger period for continuous mode

    OUTPUT_DIR = Path(args.output)

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
    )

    client = ServalClient(BASE_URL)
    client.check_connection()
    logging.debug("Connected to Serval")

    client.wait_for_detector()
    logging.debug("Camera connected to Serval")

    # Dashboard info
    dashboard = client.get_dashboard()
    logging.info(
        f"Server Software Version: {dashboard.get('Server', {}).get('SoftwareVersion')}"
    )

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

    # Set data destination
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # uncomment either (i) both (ii) stream only, (iii) file only
    destination = {
        "Raw": [
            {"Base": "tcp://connect@localhost:7070", "QueueSize": 16384},
            {"Base": Path(OUTPUT_DIR).as_uri(), "FilePattern": ""},
            ],
    }
    # destination = {
    #     "Raw": [{"Base": "tcp://connect@localhost:7070", "QueueSize": 16384}],
    # }
    # destination = {
    #     "Raw": [{"Base": Path(OUTPUT_DIR).as_uri(), "FilePattern": ""}],
    # }

    client.set_destination(destination)

    logging.info(f"Starting data taking for {N_TRIGGERS} seconds.")
    logging.info(f"Output directory: {OUTPUT_DIR}")
    start_time = time.time()

    # Start and wait for acquisition
    time.sleep(1)
    client.start_acquisition()
    client.wait_for_measurement_to_finish()

    end_time = time.time()
    duration = end_time - start_time
    logging.info(f"Acquisition took {duration:.2f} seconds.")


if __name__ == "__main__":
    main()
    
