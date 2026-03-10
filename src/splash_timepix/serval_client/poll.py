"""
Poll metadata from a running TimePix3 acquisition.
"""

from .lib import ServalClient


def main() -> None:

    client = ServalClient()
    client.check_connection()

    _ = client.get_measurement_status()


if __name__ == "__main__":
    main()
