import sitecustomize
from dataclasses import dataclass
from types import SimpleNamespace

from nexo_ai_cruise import NexoAICruiseStateManager


class ButtonType:
  unknown = 0
  accelCruise = 1
  decelCruise = 2
  cancel = 3


BUTTONS = {1: ButtonType.accelCruise, 2: ButtonType.decelCruise, 4: ButtonType.cancel}


@dataclass
class ButtonEvent:
  pressed: bool
  type: int


def create_button_events(cur_btn, prev_btn, buttons):
  events = []
  if cur_btn == prev_btn:
    return events
  if prev_btn:
    events.append(ButtonEvent(False, buttons.get(prev_btn, ButtonType.unknown)))
  if cur_btn:
    events.append(ButtonEvent(True, buttons.get(cur_btn, ButtonType.unknown)))
  return events


def car_state(speed_kph=50.0):
  return SimpleNamespace(
    vEgoCluster=speed_kph / 3.6,
    vEgo=speed_kph / 3.6,
    brakePressed=False,
    cruiseState=SimpleNamespace(available=False, enabled=False, standstill=False, speed=0.0),
  )


def manager():
  return NexoAICruiseStateManager(ButtonType, BUTTONS, create_button_events, 1.0 / 3.6, 1.609344)


def tap(mgr, cs, button):
  mgr.update(cs, 0, button, True, True, [])
  mgr.update(cs, 0, 0, True, True, [])


def arm_and_enable_med(mgr, cs):
  for _ in range(mgr.MAIN_RELEASE_ARM_FRAMES):
    mgr.update(cs, 0, 0, False, True, [])
  mgr.update(cs, 0, 0, True, True, [])
  assert mgr.available and not mgr.enabled


def test_repeated_physical_plus_minus_updates_retained_target():
  mgr = manager()
  cs = car_state()
  arm_and_enable_med(mgr, cs)

  tap(mgr, cs, 2)  # first SET captures current speed
  assert mgr.enabled
  assert mgr.speed_kph == 50.0

  tap(mgr, cs, 1)
  tap(mgr, cs, 1)
  tap(mgr, cs, 2)
  assert mgr.speed_kph == 51.0


def test_brake_returns_to_med_and_retains_speed_for_resume():
  mgr = manager()
  cs = car_state()
  arm_and_enable_med(mgr, cs)
  tap(mgr, cs, 2)
  tap(mgr, cs, 1)

  cs.brakePressed = True
  mgr.update(cs, 0, 0, True, True, [])
  assert mgr.available and not mgr.enabled
  retained = mgr.speed_kph

  cs.brakePressed = False
  mgr.update(cs, 0, 0, True, True, [])

  tap(mgr, cs, 1)
  assert mgr.available and mgr.enabled
  assert mgr.speed_kph == retained


def test_physical_cancel_exits_med_in_one_press():
  mgr = manager()
  cs = car_state()
  arm_and_enable_med(mgr, cs)
  tap(mgr, cs, 2)

  tap(mgr, cs, 4)
  assert not mgr.available and not mgr.enabled


def test_brake_pedal_event_is_suppressed_only_while_nexo_med_is_available():
  class FakeSelfdriveD:
    def update_events(self, _cs):
      self.events.events.append(10)

  fake_module = SimpleNamespace(
    SelfdriveD=FakeSelfdriveD,
    EventName=SimpleNamespace(pedalPressed=10),
  )
  sitecustomize._patch_selfdrived(fake_module)

  selfdrive = FakeSelfdriveD()
  selfdrive.CP = SimpleNamespace(carFingerprint="HYUNDAI_NEXO_1ST_GEN", openpilotLongitudinalControl=True)
  selfdrive.events = SimpleNamespace(events=[])
  cs = SimpleNamespace(
    cruiseState=SimpleNamespace(available=True),
    brakePressed=True,
    gasPressed=False,
    regenBraking=False,
  )
  selfdrive.update_events(cs)
  assert selfdrive.events.events == []

  cs.cruiseState.available = False
  selfdrive.update_events(cs)
  assert selfdrive.events.events == [10]
