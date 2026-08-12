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


def test_cancel_steps_from_speed_control_to_med_then_off():
  mgr = manager()
  cs = car_state()
  arm_and_enable_med(mgr, cs)
  tap(mgr, cs, 2)

  tap(mgr, cs, 4)
  assert mgr.available and not mgr.enabled

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


def test_card_uses_unset_speed_in_med_and_manager_speed_when_enabled():
  class FakeCar:
    def state_update(self):
      return self.CS, None

  fake_module = SimpleNamespace(Car=FakeCar)
  sitecustomize._patch_card(fake_module)

  car = FakeCar()
  car.CP = SimpleNamespace(carFingerprint="HYUNDAI_NEXO_1ST_GEN", openpilotLongitudinalControl=True)
  car.CI = SimpleNamespace(CS=SimpleNamespace(softHoldActive=2))
  car.v_cruise_helper = SimpleNamespace(
    _soft_hold_active=2,
    _activate_cruise=1,
    _paddle_decel_active=True,
    _cruise_cancel_state=True,
    _lat_enabled=False,
    v_cruise_kph=40.0,
    v_cruise_cluster_kph=40.0,
  )
  car.CS = SimpleNamespace(
    cruiseState=SimpleNamespace(available=True, enabled=False, speed=0.0),
    vCruise=40.0,
    vCruiseCluster=40.0,
    softHoldActive=2,
    activateCruise=1,
    latEnabled=False,
  )

  cs, _ = car.state_update()
  assert cs.vCruise == 255.0
  assert cs.vCruiseCluster == 255.0
  assert cs.softHoldActive == 0
  assert cs.activateCruise == 0
  assert cs.latEnabled

  cs.cruiseState.enabled = True
  cs.cruiseState.speed = 50.0 / 3.6
  cs, _ = car.state_update()
  assert cs.vCruise == 50.0
  assert cs.vCruiseCluster == 50.0
