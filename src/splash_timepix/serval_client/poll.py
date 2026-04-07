"""
One-shot Serval measurement status readout (CLI helper).

Fetches the ``Measurement`` section of the Serval HTTP dashboard once. Use this to
verify the server is reachable and inspect current acquisition metadata from a
terminal.

This is **not** a polling loop. Repeated polling is implemented in
``ServalClient.wait_for_measurement_to_finish`` / ``wait_for_detector`` in
``lib.py``, and in the UI via ``ServalPollerWorker`` (see ``ui/workers.py``).
"""

import json
import logging
import sys

from .lib import ServalClient


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    client = ServalClient()
    client.check_connection()

    status = client.get_measurement_status()
    json.dump(status, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
