"""Pinned Carrot N16 registry for the 2019 Hyundai NEXO (stage 1 only).

This tool reads source-controlled metadata and optionally hashes a user-provided
ONNX file. It never downloads, installs, enables, runs, or publishes a model.
Do not connect these results to modelV2, controls, CAN, planning or HUD rendering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MODEL_SPEC = Path(__file__).with_name('carrot_n16_spec.json')
NEXO_PROFILE = Path(__file__).with_name('nexo_n16_profile.json')
TRUSTED_SHA256 = '01effc679eb46d6036ce0302673a87fe2dcfcc95a679daeeb929679e53c3647a'
TRUSTED_NBYTES = 814591107
TRUSTED_FRAME_SKIP = 4
TRUSTED_UPSTREAM_BLOB = '22cd8351ea1596f85c1bca7e75d0f979e87d98ae'

EXPECTED_INPUTS = {
    'new_img', 'desire', 'traffic_convention', 'action_t', 'nav_map',
    'navigation', 'state_img_q', 'state_desire_q', 'state_feat_q',
    'state_map_features_q', 'state_navigation_features_q',
}
EXPECTED_OUTPUTS = {
    'outputs', 'next_state_img_q', 'next_state_desire_q',
    'next_state_feat_q', 'next_state_map_features_q',
    'next_state_navigation_features_q',
}


def load_registry() -> tuple[dict, dict]:
  spec = json.loads(MODEL_SPEC.read_text(encoding='utf-8'))
  profile = json.loads(NEXO_PROFILE.read_text(encoding='utf-8'))
  if (
      spec.get('sha256') != TRUSTED_SHA256
      or spec.get('nbytes') != TRUSTED_NBYTES
      or spec.get('frame_skip') != TRUSTED_FRAME_SKIP
      or spec.get('input_shapes', {}).keys() != EXPECTED_INPUTS
      or spec.get('output_shapes', {}).keys() != EXPECTED_OUTPUTS
      or spec.get('output_shapes', {}).get('outputs') != [1, 18580]
      or spec.get('input_shapes', {}).get('nav_map') != [1, 1, 256, 256]
      or spec.get('input_shapes', {}).get('navigation') != [1, 47]
      or spec.get('output_slices', {}).get('lead') != [1907, 2051]
      or spec.get('output_slices', {}).get('plan') != [917, 1907]
      or spec.get('output_slices', {}).get('hidden_state') != [2066, 18450]
      or profile.get('expected_model_sha256') != TRUSTED_SHA256
      or profile.get('expected_model_bytes') != TRUSTED_NBYTES
      or profile.get('upstream_source_blob_sha') != TRUSTED_UPSTREAM_BLOB
      or profile.get('vehicle') != 'Hyundai NEXO (FE), 2019'
      or profile.get('phase') != 'stage_1_source_registration'
  ):
    raise ValueError('Carrot N16 manifest/provenance or 2019 NEXO profile mismatch')
  for shapes in (spec['input_shapes'], spec['output_shapes']):
    if any(not isinstance(shape, list) or not shape or any(type(n) is not int or n <= 0 for n in shape)
           for shape in shapes.values()):
      raise ValueError('Invalid N16 tensor shapes')
  slices = spec['output_slices']
  if (
      not isinstance(slices, dict)
      or any(not isinstance(value, list) or len(value) != 2 or
             any(type(v) is not int for v in value) or
             value[0] < 0 or value[1] <= value[0] or value[1] > 18580
             for value in slices.values())
      or sorted(slices.values()) != [[start, stop] for start, stop in
                                     sorted((v[0], v[1]) for v in slices.values())]
      or sorted(slices.values())[0][0] != 0
      or sorted(slices.values())[-1][1] != 18580
      or any(a[1] != b[0] for a, b in zip(sorted(slices.values()), sorted(slices.values())[1:]))
  ):
    raise ValueError('Invalid or non-contiguous N16 output slices')
  if (
      type(profile.get('enabled')) is not bool or profile['enabled']
      or profile.get('on_device_installed') is not False
      or profile.get('display_only') is not True
      or profile.get('hud_output_enabled') is not False
      or profile.get('vehicle_control_enabled') is not False
      or profile.get('hud_geometry_verified') is not False
      or not isinstance(profile.get('safety'), dict)
      or any(value is not True for key, value in profile['safety'].items()
             if key in {'production_model_unchanged', 'modeld_and_JoiningModel_unchanged',
                        'offroad_only_for_future_inference_tests'})
      or any(value is not False for key, value in profile['safety'].items()
             if key in {'can_send_disabled', 'steering_disabled', 'acceleration_disabled',
                        'braking_disabled', 'auto_download', 'auto_update', 'auto_install',
                        'auto_enable', 'live_hud_feed'})
  ):
    raise ValueError('NEXO observer profile must remain disabled and non-controlling')
  return spec, profile


def hash_matches(path: Path, *, sha256: str, nbytes: int) -> bool:
  """Streaming exact digest and byte count; no ONNX import or GPU access."""
  if path.is_symlink() or not path.is_file():
    return False
  try:
    if path.stat().st_size != nbytes:
      return False
    digest = hashlib.sha256()
    length = 0
    with path.open('rb') as source:
      while block := source.read(4 * 1024 * 1024):
        digest.update(block)
        length += len(block)
    return length == nbytes and digest.hexdigest() == sha256
  except OSError:
    return False


def report(model_path: Path | None = None) -> dict:
  spec, profile = load_registry()
  status = {
      'phase': 'stage_1_source_registration',
      'state': 'registered_not_installed',
      'vehicle': profile['vehicle'],
      'upstream': profile['upstream_repository'],
      'model_sha256': spec['sha256'],
      'model_bytes': spec['nbytes'],
      'input_names': sorted(spec['input_shapes']),
      'output_names': sorted(spec['output_shapes']),
      'model_inference_verified': False,
      'tensorrt_engine_verified': False,
      'real_camera_verified': False,
      'hud_geometry_verified': False,
      'on_device_installed': False,
      'vehicle_control_enabled': False,
      'hud_output_enabled': False,
      'display_only': True,
      'note': 'This is a source registry only. Do not enable N16 or connect to vehicle control.'
  }
  if model_path is not None:
    matches = hash_matches(model_path, sha256=TRUSTED_SHA256, nbytes=TRUSTED_NBYTES)
    status['local_file_verified'] = matches
    status['state'] = 'local_artifact_verified_not_installed' if matches else 'local_artifact_invalid'
  return status


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--verify-model', type=Path, default=None,
                      help='Optional existing local ONNX file; only hash/size is checked.')
  args = parser.parse_args()
  try:
    result = report(args.verify_model)
  except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
    print(json.dumps({'state': 'registration_invalid', 'error': str(error),
                      'vehicle_control_enabled': False, 'on_device_installed': False}))
    return 1
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 1 if result['state'] == 'local_artifact_invalid' else 0


if __name__ == '__main__':
  raise SystemExit(main())
