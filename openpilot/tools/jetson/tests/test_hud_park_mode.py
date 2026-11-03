"""Verify the separate parked-onroad Display Only HUD installer argument."""
import hashlib
import json
import sys

from openpilot.tools.jetson.install_hud_update import install_command


def test_display_only_hud_parked_flag_keeps_verified_model_config(tmp_path):
  directory = tmp_path / 'package'
  directory.mkdir()
  bundle = directory / 'nexo-hud-repair.tar.gz'
  bundle.write_bytes(b'protected test bundle')
  for file in ('protected_install.py', 'protected_ncm.py'):
    (directory / file).write_text('# placeholder')
  config = tmp_path / 'nexo-ncm.json'
  config.write_text(json.dumps({'display_only': True, 'transport': 'ncm-tcp',
                                'interface': 'usb0', 'model_sha256': 'a' * 64}))
  digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
  command = install_command(directory, bundle, digest, config=config, confirmed_parked=True)
  assert command[:2] == [sys.executable, str(directory / 'protected_install.py')]
  assert '--display-only' in command
  assert '--confirmed-parked' in command
  assert '--confirmed-offroad' not in command
  assert command[command.index('--model-sha256') + 1] == 'a' * 64


def test_manual_install_still_requires_existing_offroad_confirmation(tmp_path):
  directory = tmp_path / 'package'
  directory.mkdir()
  bundle = directory / 'nexo-hud-repair.tar.gz'
  bundle.write_bytes(b'protected test bundle')
  for file in ('protected_install.py', 'protected_ncm.py'):
    (directory / file).write_text('# placeholder')
  config = tmp_path / 'nexo-ncm.json'
  config.write_text(json.dumps({'display_only': True, 'transport': 'ncm-tcp',
                                'interface': 'usb0', 'model_sha256': 'b' * 64}))
  digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
  command = install_command(directory, bundle, digest, config=config)
  assert '--confirmed-offroad' in command
  assert '--confirmed-parked' not in command
