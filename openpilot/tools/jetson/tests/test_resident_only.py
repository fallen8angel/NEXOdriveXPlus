"""A live ENGINE_REQ cannot invoke the pinned builder or switch host.loaded."""
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest

from openpilot.tools.jetson import native_host
from openpilot.selfdrive.modeld.jetlink.protocol import Msg
from openpilot.tools.jetson.transport.base import Message


@pytest.mark.parametrize('resident', [False, True])
def test_live_bind_is_atomic_read_only_and_never_calls_host_request(tmp_path, monkeypatch, resident):
  monkeypatch.syspath_prepend(str(Path(os.environ['CARROT_JETSON']) / 'third_party/jetlink'))
  monkeypatch.setattr(native_host, 'SNAPSHOT', tmp_path / 'snapshot')
  wanted = {'sha256': 'a' * 64, 'nbytes': 32, 'frame_skip': 4}
  spec = NS(nbytes=32, frame_skip=4, to_dict=lambda: wanted)
  loaded = NS(sha256='a' * 64, spec=spec) if resident else None
  host = NS(lock=threading.RLock(), loaded=loaded, session=None,
    request=lambda *a: pytest.fail('live path attempted to compile/switch an engine'))
  replies = []
  class Base:
    def __init__(self):
      self.host = host
      self.telemetry = NS(health={})
    def _send_json(self, *args):
      replies.append(args)
  owner = native_host.session_class(Base)()
  owner.on_engine_req(Message(Msg.ENGINE_REQ, 1, 0, memoryview(json.dumps({**wanted, 'resident_only': True}).encode())))
  assert replies[0][2]['state'] == ('ready' if resident else 'not_resident')
  assert host.loaded is loaded
  assert (owner.request is not None) == resident


def test_live_backend_never_compiles_even_if_preload_or_control_requests_it():
  class Backend:
    def load(self, path):
      return path
    def build(self, *args, **kwargs):
      pytest.fail('original builder must not run in live server')
  checked = native_host.resident_backend(Backend)()
  assert checked.load('verified-existing.plan') == 'verified-existing.plan'
  with pytest.raises(RuntimeError, match='Offroad'):
    checked.build('model.onnx', 'new.plan')
