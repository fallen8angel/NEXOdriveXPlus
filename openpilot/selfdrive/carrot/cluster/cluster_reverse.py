"""Reverse HUD presentation contract. No camera/device or vehicle control writes."""
from dataclasses import dataclass, replace
import math
import time
from typing import Literal



@dataclass(frozen=True, slots=True)
class RearSensor:
    # Physical position must be validated before connecting a CAN channel.
    position: str
    lateral: float  # -1 = left edge of rear bumper, +1 = right edge
    detected: bool | None = None
    proximity: Literal['far', 'near', 'very_near'] | None = None


@dataclass(frozen=True, slots=True)
class RearParkingState:
    sensors: tuple[RearSensor, ...] = ()
    # Diagnostic candidates, deliberately not interpreted as physical sensors.
    raw_codes: tuple[tuple[str, int], ...] = ()
    received_t: float | None = None


def with_reverse_hud_state(state, valid: bool, rear: RearParkingState):
    # Same started + reverse predicate as mici/reverse_camera_state.py.
    # Do not import UI packages: ui/__init__.py installs onroad renderer hooks.
    active = bool(state.onroad and valid and state.gear_text == 'R')
    return replace(state, reverse_active=active, rear_parking=rear if active else RearParkingState())


def sensor_style(sensor: RearSensor):
    if sensor.detected is not True or not math.isfinite(sensor.lateral) or abs(sensor.lateral) > 1:
        return None
    # No code-to-distance mapping. Only a validated adapter may set proximity.
    return {
        'far': ((55, 225, 83, 255), 1),
        'near': ((255, 214, 40, 255), 2),
        'very_near': ((255, 55, 48, 255), 3),
    }.get(sensor.proximity, ((255, 180, 0, 255), 2))


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
                print(f'Cluster reverse camera cleanup failed: {exc}', flush=True)

    def draw(self, destination):
        now = self.clock()
        if now < self.retry_at:
            return False
        try:
            if self.camera is None:
                self.camera = self.factory()
            return bool(self.camera.draw(destination))
        except Exception as exc:
            self.close()
            self.retry_at = now + 1.0
            print(f'Cluster reverse camera waiting: {exc}', flush=True)
            return False
