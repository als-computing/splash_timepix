"""
Poll metadata from a running TimePix3 acquisition.
"""
from .lib import ServalError, ServalClient


def main() -> None:
    BASE_URL = "http://localhost:8080"

    client = ServalClient(BASE_URL)
    client.check_connection()

    meta = client.get_measurement_status()

    frame_count = meta.get("FrameCount", 0)
    dropped_frames = meta.get("DroppedFrames", 0)
    elapsed_time = meta.get("ElapsedTime", 0.0)
    time_left = meta.get("TimeLeft", 0.0)
    status = meta.get("Status", "UNKNOWN")
    pixel_event_rate = meta.get("PixelEventRate", 0)
    tdc1_event_rate = meta.get("Tdc1EventRate", 0)
    tdc2_event_rate = meta.get("Tdc2EventRate", 0)


if __name__ == "__main__":
    main()
    