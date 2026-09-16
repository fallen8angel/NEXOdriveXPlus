from types import SimpleNamespace

from opendbc.car.hyundai.carstate import PARKING_SENSOR_TIMEOUT_NS, _get_nexo_parking_sensor_state


def _parser(message, values, timestamp=1_000_000_000, age=0):
  timestamp_signal = "CF_Gway_PASSystemOn" if message == "PAS11" else "CF_Spas_HMI_Stat"
  return SimpleNamespace(
    bus_timeout=False,
    ts_nanos={message: {timestamp_signal: timestamp}},
    _last_update_nanos=timestamp + age,
    vl={message: values},
  )


def test_pas11_parking_sensor_positions_are_preserved() -> None:
  cp = _parser("PAS11", {
    "CF_Gway_PASDisplayFLH": 1,
    "CF_Gway_PASDisplayFCTR": 2,
    "CF_Gway_PASDisplayFRH": 3,
    "CF_Gway_PASDisplayRLH": 4,
    "CF_Gway_PASDisplayRCTR": 5,
    "CF_Gway_PASDisplayRRH": 6,
    "CF_Gway_PASSystemOn": 1,
  })

  assert _get_nexo_parking_sensor_state((cp,)) == (True, True, 1, 2, 3, 4, 5, 6)


def test_spas12_inner_and_outer_positions_use_nearest_level() -> None:
  cp = _parser("SPAS12", {
    "CF_Spas_HMI_Stat": 1,
    "CF_Spas_Disp": 1,
    "CF_Spas_FOL_Ind": 2,
    "CF_Spas_FIL_Ind": 5,
    "CF_Spas_FI_Ind": 3,
    "CF_Spas_FOR_Ind": 4,
    "CF_Spas_FIR_Ind": 1,
    "CF_Spas_ROL_Ind": 1,
    "CF_Spas_RIL_Ind": 6,
    "CF_Spas_RI_Ind": 2,
    "CF_Spas_ROR_Ind": 3,
    "CF_Spas_RIR_Ind": 7,
  })

  assert _get_nexo_parking_sensor_state((cp,)) == (True, True, 5, 3, 4, 6, 2, 7)


def test_stale_parking_sensor_message_is_invalid() -> None:
  cp = _parser(
    "PAS11",
    {},
    age=PARKING_SENSOR_TIMEOUT_NS + 1,
  )

  assert _get_nexo_parking_sensor_state((cp,)) == (False, False, 0, 0, 0, 0, 0, 0)
