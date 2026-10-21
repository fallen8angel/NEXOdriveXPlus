"""Explicit parked TCP HELLO/STATE check; no engine request or device mutation."""
import argparse
import hashlib
import json

from openpilot.selfdrive.modeld.jetlink import enabled
from openpilot.selfdrive.modeld.jetlink.client import JetlinkClient
from openpilot.selfdrive.modeld.jetlink.ncm_transport import JetlinkNcmTcpTransport, JETSON_ADDR, PORT
from openpilot.selfdrive.modeld.jetlink.spec import NATIVE_MODEL
from openpilot.tools.jetson.ncm_display import OPT_IN


def require_idle(params):
  if not params.get_bool('IsOffroad') or params.get_bool('IsOnroad'):
    raise RuntimeError('Parked/offroad state is required')
  if enabled() or OPT_IN.exists() or params.get_bool('NexoJetsonUsb'):
    raise RuntimeError('Disable inference and both display owners before this one-shot check')


def inspect_peer(client, expected_sha):
  peer = client.hello(timeout=2)
  state = client.state(timeout=1)
  if not isinstance(state, dict):
    raise ValueError('Invalid Jetlink STATE response')
  loaded = state.get('loaded')
  result = {'transport': 'ncm-tcp', 'endpoint': f'{JETSON_ADDR}:{PORT}', 'protocol': peer.get('protocol'),
            'hello_verified': True, 'state_verified': True, 'loaded_sha256': loaded,
            'expected_sha256': expected_sha, 'model_sha_matches': loaded == expected_sha,
            'backend': peer.get('backend'), 'nexo_hud_supported': peer.get('nexo_hud_v1') is True,
            'display_only': peer.get('nexo_display_only_v1') is True,
            'engine_contract_verified': False, 'inference_verified': False, 'physical_hud_verified': False}
  # HELLO/STATE can identify the resident model but do not authorize inference.
  # The existing daemon still verifies the full ENGINE spec and reset warmups.
  result['eligible'] = loaded == expected_sha and peer.get('backend') == 'trt' and not result['display_only']
  return result


def check(params):
  require_idle(params)
  expected = hashlib.sha256(NATIVE_MODEL.read_bytes()).hexdigest()
  require_idle(params)
  transport = JetlinkNcmTcpTransport.connect(timeout=2)
  client = JetlinkClient(transport, name='nexo-ncm-check')
  try:
    require_idle(params)
    return inspect_peer(client, expected)
  finally:
    client.close()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--confirmed-offroad', action='store_true', required=True)
  parser.parse_args()
  from openpilot.common.params import Params
  try:
    result = check(Params())
  except Exception as error:
    print(json.dumps({'eligible': False, 'error': str(error),
                      'hint': 'Verify protected NCM installation and TCP 192.168.100.250:5599; do not unbind g1.'}))
    return 2
  print(json.dumps(result, indent=2))
  return 0 if result['eligible'] else 2


if __name__ == '__main__':
  raise SystemExit(main())
