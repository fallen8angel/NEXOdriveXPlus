"""Reverse HUD presentation contract. No camera/device or vehicle control writes."""
from dataclasses import dataclass, replace
import math
import time
from typing import Literal


ParkingProximity = Literal["far", "near", "very_near"]
ParkingEnd = Literal["front", "rear"]


@dataclass(frozen=True, slots=True)
class RearSensor:
    """One physical SPAS indication position.

    The historical name RearSensor is retained for compatibility. The end field
    tells the renderer whether the sensor belongs to the front or rear bumper.
    """

    position: str
    lateral: float  # -1 = left edge, 0 = center, +1 = right edge
    detected: bool | None = None
    proximity: ParkingProximity | None = None
    end: ParkingEnd = "rear"
    code: int = 0


@dataclass(frozen=True, slots=True)
class RearParkingState:
    # Rear sensors stay in sensors for compatibility with the first reverse HUD.
    sensors: tuple[RearSensor, ...] = ()
    front_sensors: tuple[RearSensor, ...] = ()
    raw_codes: tuple[tuple[str, int], ...] = ()
    received_t: float | None = None


def with_reverse_hud_state(state, valid: bool, rear: RearParkingState):
    # The dedicated external-HUD reverse screen has been retired.
    # Keep the normal external HUD layout in R as well; Comma/Mici reverse-camera
    # behavior is separate and remains unchanged.
    return replace(state, reverse_active=False, rear_parking=RearParkingState())


def sensor_style(sensor: RearSensor):
    if sensor.detected is not True or not math.isfinite(sensor.lateral) or abs(sensor.lateral) > 1:
        return None
    # SPAS12 1/2/3 are used as display stages, not calibrated centimetre distances.
    return {
        "far": ((55, 225, 83, 255), 1),
        "near": ((255, 214, 40, 255), 2),
        "very_near": ((255, 55, 48, 255), 3),
    }.get(sensor.proximity, ((255, 180, 0, 255), 1))


def contained_rect(width, height, bounds):
    x, y, w, h = bounds
    if min(width, height, w, h) <= 0:
        return (x, y, 0.0, 0.0)
    scale = min(w / width, h / height)
    dw, dh = width * scale, height * scale
    return (x + (w - dw) / 2, y + (h - dh) / 2, dw, dh)


class ReverseCameraSession:
    """One read-only IPC subscriber; all methods run on the render thread."""
    def __init__(self, factory, clock=time.monotonic):
        self.factory = factory
        self.clock = clock
        self.camera = None
        self.retry_at = 0.0

    def close(self):
        camera, self.camera = self.camera, None
        self.retry_at = 0.0
        if camera is not None:
            try:
                camera.close()
            except Exception as exc:
                print(f"Cluster reverse camera cleanup failed: {exc}", flush=True)

    def draw(self, destination, **draw_kwargs):
        now = self.clock()
        if now < self.retry_at:
            return False
        try:
            if self.camera is None:
                self.camera = self.factory()
            return bool(self.camera.draw(destination, **draw_kwargs))
        except Exception as exc:
            self.close()
            self.retry_at = now + 1.0
            print(f"Cluster reverse camera waiting: {exc}", flush=True)
            return False
