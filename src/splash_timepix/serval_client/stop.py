"""
Stop a running TimePix3 acquisition gracefully.
"""
import logging
from .lib import ServalError, ServalClient


def main() -> None:

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
    )

    client = ServalClient()
    client.check_connection()
    client.stop_acquisition()


if __name__ == "__main__":
    main()
    