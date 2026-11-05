"""Protected Jetson DATA SSH public-key persistence, no real SSH/root mounts."""
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.tools.jetson import protected_install as protected


def key():
  # Structurally valid synthetic Ed25519 public key, never an operator secret.
  blob = b'\x00\x00\x00\x0bssh-ed25519' + b'\x00\x00\x00\x20' + b'x' * 32
  return ('ssh-ed25519 ' + base64.b64encode(blob).decode() + ' comma4-to-jetson\n').encode('ascii')


@pytest.fixture
def fixture(tmp_path, monkeypatch):
  root = tmp_path / 'persistent'
  (root / 'updater').mkdir(parents=True)
  home = tmp_path / 'home/jetlink'
  home.mkdir(parents=True)
  monkeypatch.setattr(protected.pwd, 'getpwnam',
                      lambda name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
  monkeypatch.setattr(protected.os, 'chown', lambda *_: None)
  seed = root / 'updater/nexo-comma-authorized-key.pub'
  seed.write_bytes(key())
  seed.chmod(0o600)
  return root, home, seed


def check(root, home):
  return protected.restore_boot_ssh_key(root, home, expected_seed_owner=os.getuid())


def test_restore_key_preserves_factory_windows_key_and_is_idempotent(fixture):
  root, home, seed = fixture
  folder = home / '.ssh'
  folder.mkdir()
  authorized = folder / 'authorized_keys'
  original = b'ssh-ed25519 AAAAFactoryWindowKey human@desktop\n'
  authorized.write_bytes(original)
  assert check(root, home) is True
  value = authorized.read_bytes()
  assert value.startswith(original)
  assert value.endswith(key())
  assert check(root, home) is False
  assert authorized.read_bytes() == value
  assert authorized.stat().st_mode & 0o777 == 0o600
  assert folder.stat().st_mode & 0o777 == 0o700


def test_recreates_missing_volatile_ssh_directory_on_boot(fixture):
  root, home, seed = fixture
  assert check(root, home) is True
  assert (home / '.ssh/authorized_keys').read_bytes() == key()


@pytest.mark.parametrize('invalid', [
  b'', b'ssh-rsa AAAA obsolete\n', b'command="evil" ssh-ed25519 AAAA unwanted\n',
  b'ssh-ed25519 invalid comma4-to-jetson\n', b'ssh-ed25519 AAAA another-user\n',
  b'\xff\xfe\n',
])
def test_bad_persisted_key_never_touches_authorized_keys(fixture, invalid):
  root, home, seed = fixture
  seed.write_bytes(invalid)
  with pytest.raises(ValueError):
    check(root, home)
  assert not (home / '.ssh/authorized_keys').exists()


def test_world_writable_seed_rejected(fixture):
  root, home, seed = fixture
  seed.chmod(0o666)
  with pytest.raises(ValueError, match='root-owned regular file'):
    check(root, home)


def test_seed_symlink_rejected(fixture):
  root, home, seed = fixture
  original = root / 'safe'
  original.write_bytes(key())
  seed.unlink()
  seed.symlink_to(original)
  with pytest.raises(ValueError):
    check(root, home)


def test_home_symlink_rejected(fixture):
  root, home, seed = fixture
  original = home.parent / 'actual'
  original.mkdir()
  home.rmdir()
  home.symlink_to(original)
  with pytest.raises(ValueError, match='home'):
    check(root, home)


def test_authorized_keys_symlink_rejected(fixture):
  root, home, seed = fixture
  folder = home / '.ssh'
  folder.mkdir()
  (folder / 'authorized_keys').symlink_to(seed)
  with pytest.raises(ValueError, match='authorized_keys'):
    check(root, home)
  assert seed.read_bytes() == key()


def test_seed_missing_keeps_factory_keys_without_writes(fixture):
  root, home, seed = fixture
  seed.unlink()
  assert check(root, home) is False
  assert not (home / '.ssh').exists()


def test_updater_activate_restores_key_before_boot_services(device, monkeypatch):
  called = []
  monkeypatch.setattr(protected, 'restore_boot_ssh_key', lambda: called.append('restore') or False)
  protected.updater('activate')
  assert called == ['restore']
