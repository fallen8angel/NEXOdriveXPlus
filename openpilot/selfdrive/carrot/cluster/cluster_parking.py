"""Receive-only NEXO SPAS12 parking display.

SPAS12 exposes five front and five rear indication positions. Codes 1/2/3 are
rendered as three display stages (far/near/very_near). They are not calibrated
centimetre distances. Values 0 and 4..7 are treated as no validated indication.
"""
from dataclasses import dataclass
import math

from cluster_reverse import RearParkingState, RearSensor


NEXO_FINGERPRINT = "HYUNDAI_NEXO_1ST_GEN"
PARKING_TIMEOUT_S = 1.5
VALID_CODES = (1, 2, 3)
# Factory-cluster 3-zone alarm stages are a receive-only fallback when
# individual indication fields stay zero on some NEXO SPAS firmware.
FRONT_ALARM_STARTS = (30, 38, 46)  # FLS, FCS, FRS
REAR_ALARM_STARTS = (57, 59, 61)   # RLS, RCS, RRS
PROXIMITY_BY_CODE = {1: "far", 2: "near", 3: "very_near"}

# Spatial order is left -> right as viewed from above the vehicle.
FRONT_LAYOUT = (
    ("FOL", 16, -0.90),
    ("FIL", 10, -0.45),
    ("FI", 40, 0.00),
    ("FIR", 13, 0.45),
    ("FOR", 19, 0.90),
)
REAR_LAYOUT = (
    ("ROL", 32, -0.90),
    ("RIL", 24, -0.45),
    ("RI", 43, 0.00),
    ("RIR", 27, 0.45),
    ("ROR", 35, 0.90),
)


@dataclass(frozen=True, slots=True)
class ParkingIndications:
    # Legacy aggregate flags retained for callers that only need four corners.
    front_left: bool = False
    front_right: bool = False
    rear_left: bool = False
    rear_right: bool = False
    # Individual SPAS12 codes, left -> right, five positions per bumper.
    front_codes: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)
    rear_codes: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)


def _code(bits: int, start: int) -> int:
    value = (bits >> start) & 7
    return value if value in VALID_CODES else 0


def _sensor(name: str, lateral: float, code: int, end: str) -> RearSensor:
    return RearSensor(
        position=name,
        lateral=lateral,
        detected=code in VALID_CODES,
        proximity=PROXIMITY_BY_CODE.get(code),
        end=end,
        code=code,
    )


class NexoParkingTracker:
    def __init__(self):
        self.clear()

    def clear(self):
        self.indications = ParkingIndications()
        self.received_t = None
        self.rear = RearParkingState()

    def observe(self, frames, event_t: float, now: float, valid: bool = True):
        if not valid:
            self.clear()
            return
        if not math.isfinite(event_t) or not 0 <= now - event_t <= PARKING_TIMEOUT_S:
            return

        for frame in frames:
            # Bus 0 is the NEXO physical RX source in supplied captures.
            # Ignore forwarding/TX echoes and other buses.
            if int(frame.address) != 0x4F4 or int(frame.src) != 0:
                continue

            data = bytes(frame.dat)
            if len(data) != 8:
                self.clear()
                continue
            if self.received_t is not None and event_t < self.received_t:
                continue

            bits = int.from_bytes(data, "little")
            front_codes = tuple(_code(bits, start) for _, start, _ in FRONT_LAYOUT)
            rear_codes = tuple(_code(bits, start) for _, start, _ in REAR_LAYOUT)

            # The factory cluster can drive its six parking zones from these
            # alarm fields while individual Ind fields remain zero. Merge them
            # only as fallback; proven individual sensor indications win.
            front_alarms = tuple((bits >> start) & 3 for start in FRONT_ALARM_STARTS)
            rear_alarms = tuple((bits >> start) & 3 for start in REAR_ALARM_STARTS)
            front_codes = list(front_codes)
            rear_codes = list(rear_codes)
            for index, alarm in zip((1, 2, 3), front_alarms, strict=True):
                if front_codes[index] == 0 and alarm in VALID_CODES:
                    front_codes[index] = alarm
            for index, alarm in zip((1, 2, 3), rear_alarms, strict=True):
                if rear_codes[index] == 0 and alarm in VALID_CODES:
                    rear_codes[index] = alarm
            front_codes = tuple(front_codes)
            rear_codes = tuple(rear_codes)

            self.indications = ParkingIndications(
                front_left=any(front_codes[i] in VALID_CODES for i in (0, 1)),
                front_right=any(front_codes[i] in VALID_CODES for i in (3, 4)),
                rear_left=any(rear_codes[i] in VALID_CODES for i in (0, 1)),
                rear_right=any(rear_codes[i] in VALID_CODES for i in (3, 4)),
                front_codes=front_codes,
                rear_codes=rear_codes,
            )
            self.received_t = event_t

            front_sensors = tuple(
                _sensor(name, lateral, code, "front")
                for (name, _, lateral), code in zip(FRONT_LAYOUT, front_codes, strict=True)
            )
            rear_sensors = tuple(
                _sensor(name, lateral, code, "rear")
                for (name, _, lateral), code in zip(REAR_LAYOUT, rear_codes, strict=True)
            )
            self.rear = RearParkingState(
                sensors=rear_sensors,
                front_sensors=front_sensors,
                raw_codes=tuple(
                    [(name, code) for (name, _, _), code in zip(FRONT_LAYOUT, front_codes, strict=True)]
                    + [(name, code) for (name, _, _), code in zip(REAR_LAYOUT, rear_codes, strict=True)]
                ),
                received_t=event_t,
            )

    def current(self, now: float) -> ParkingIndications:
        if self.received_t is None or not 0 <= now - self.received_t <= PARKING_TIMEOUT_S:
            return ParkingIndications()
        return self.indications

    def current_rear(self, now: float) -> RearParkingState:
        if self.received_t is None or not 0 <= now - self.received_t <= PARKING_TIMEOUT_S:
            return RearParkingState()
        return self.rear
