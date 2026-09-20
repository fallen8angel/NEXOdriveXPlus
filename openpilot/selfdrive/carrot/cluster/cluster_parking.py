"""Receive-only NEXO parking display, limited to SPAS12 values seen in captures.

Positions follow hyundai_kia_generic.dbc. Values 1..3 are indication codes,
not calibrated distances or a confirmed urgency ordering. PAS11 and unobserved
outer-front/outer-left signals are intentionally not used in this first display.
"""
from dataclasses import dataclass
import math
from cluster_reverse import RearParkingState


NEXO_FINGERPRINT = "HYUNDAI_NEXO_1ST_GEN"
PARKING_TIMEOUT_S = 1.0


@dataclass(frozen=True, slots=True)
class ParkingIndications:
    front_left: bool = False
    front_right: bool = False
    rear_left: bool = False
    rear_right: bool = False


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
            # Bus 0 is the NEXO physical RX source in the supplied captures.
            # Ignore forwarding/TX echo src 128/130 and all other buses.
            if int(frame.address) != 0x4F4 or int(frame.src) != 0:
                continue
            data = bytes(frame.dat)
            if len(data) != 8:
                self.clear()
                continue
            if self.received_t is not None and event_t < self.received_t:
                continue
            bits = int.from_bytes(data, "little")
            def active(start):
                return ((bits >> start) & 7) in (1, 2, 3)
            self.indications = ParkingIndications(active(10), active(13), active(24), active(27) or active(35))
            self.received_t = event_t
            self.rear = RearParkingState(raw_codes=tuple(
                (name, (bits >> start) & 7) for name, start in
                (("ROL", 32), ("RIL", 24), ("RIR", 27), ("ROR", 35), ("RI", 43))
            ), received_t=event_t)

    def current(self, now: float) -> ParkingIndications:
        if self.received_t is None or not 0 <= now - self.received_t <= PARKING_TIMEOUT_S:
            return ParkingIndications()
        return self.indications

    def current_rear(self, now: float) -> RearParkingState:
        if self.received_t is None or not 0 <= now - self.received_t <= PARKING_TIMEOUT_S:
            return RearParkingState()
        return self.rear
