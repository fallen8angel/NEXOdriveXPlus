"""Compatibility reads on real small images; no device or write-capable handle."""
import base64
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from openpilot.tools.jetson import protected_card_check as check


def card(tmp_path, variant=0, patched=False):
  start, length, offset = 2048, 4096, 2560
  root = bytearray(b'a' * length)
  alternate = bytearray(root)
  alternate[0] = ord('b')
  def encode(data):
    return base64.b64encode(data).decode()
  old, new = b'a' * 512, b'z' * 512
  extent = {'offset': offset, 'before': encode(old), 'after': encode(new),
            'before_sha256': hashlib.sha256(old).hexdigest(), 'after_sha256': hashlib.sha256(new).hexdigest()}
  profiles = [{'root_offset': start, 'root_bytes': length, 'image_bytes': start+length,
               'partition_guard': {'offset': 1024, 'data': encode(b'g' * 512)},
               'patches': [extent], 'root_sha256': hashlib.sha256(data).hexdigest(),
               'release': name} for data, name in ((root, 'r2-original'), (alternate, 'r2-sd-nvme-v2'))]
  index = {'format': 'carrot-offline-boot-v1', 'profiles': profiles}
  image = bytearray(start+length)
  image[1024:1536] = b'g' * 512
  image[start:] = root if variant == 0 else alternate
  if patched:
    image[offset:offset+512] = new
  target = tmp_path / 'card.img'
  target.write_bytes(image)
  return target, index


@pytest.mark.parametrize('variant', [0, 1])
@pytest.mark.parametrize('patched', [False, True])
def test_selects_real_image_and_never_opens_for_write(tmp_path, monkeypatch, variant, patched):
  target, index = card(tmp_path, variant, patched)
  before = target.read_bytes()
  original = Path.open
  modes = []

  def readonly(path, mode='r', **kwargs):
    assert mode == 'rb'
    modes.append(mode)
    return original(path, mode, **kwargs)

  with monkeypatch.context() as context:
    context.setattr(Path, 'open', readonly)
    result = check.inspect_card(target, index, report=lambda _: None)
  assert result['state'] == 'supported_app_profile'
  assert result['profile'] == index['profiles'][variant]['release']
  assert result['card_written'] is False and result['installation_started'] is False
  assert modes == ['rb']
  assert target.read_bytes() == before


def test_unrelated_root_change_does_not_claim_support(tmp_path):
  target, index = card(tmp_path)
  changed = bytearray(target.read_bytes())
  changed[5000] ^= 1
  target.write_bytes(changed)
  result = check.inspect_card(target, index, report=lambda _: None)
  assert result['state'] == 'unsupported_app_profile' and result['profile'] is None
  assert target.read_bytes() == changed


def test_wrong_partition_guard_refused_without_writes(tmp_path):
  target, index = card(tmp_path)
  changed = bytearray(target.read_bytes())
  changed[1024] ^= 1
  target.write_bytes(changed)
  with pytest.raises(ValueError, match='partition layout'):
    check.inspect_card(target, index)
  assert target.read_bytes() == changed


def test_truncated_read_does_not_claim_support(tmp_path):
  target, index = card(tmp_path)
  target.write_bytes(target.read_bytes()[:-1])
  with pytest.raises(ValueError, match='Short card read'):
    check.inspect_card(target, index, report=lambda _: None)


def test_variant_normalization_mismatch_refused(tmp_path):
  _, index = card(tmp_path)
  damaged = deepcopy(index)
  damaged['profiles'][1]['patches'] = []
  with pytest.raises(ValueError, match='normalization mismatch'):
    check.validate_index(damaged)
