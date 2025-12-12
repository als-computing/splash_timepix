"""
Stop a running TimePix3 acquisition gracefully.
"""
import logging
from lib import ServalError, ServalClient


def main() -> None:
    BASE_URL = "http://localhost:8080"

    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
    )

    client = ServalClient(BASE_URL)
    client.check_connection()
    client.stop_acquisition()


if __name__ == "__main__":
    main()