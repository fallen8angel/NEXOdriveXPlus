"""Protected Jetson candidate: real host/HUD imports and finite NEXO TensorRT inference."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]


def main():
  vendor = ROOT / 'carrot-server'
  sys.path[:0] = [str(ROOT), str(vendor / 'tools/jetlink'), str(vendor / 'third_party/jetlink')]
  os.environ['CARROT_JETSON'] = str(vendor)
  import numpy as np
  import tensorrt
  from jetlink.spec import spec_from_onnx
  from jetlink.server.backends.trt import TrtBackend
  from jetlink.server.cache import EngineCache
  from jetlink.server.session import EngineHost, Request
  from openpilot.tools.jetson.native_host import session_class
  module_spec = importlib.util.spec_from_file_location('nexo_carrot_server', vendor / 'tools/jetlink/server.py')
  module = importlib.util.module_from_spec(module_spec)
  module_spec.loader.exec_module(module)
  assert issubclass(session_class(module.CarrotSession), module.CarrotSession)
  sys.path.insert(0, str(ROOT / 'openpilot/selfdrive/carrot/cluster'))
  from openpilot.tools.jetson.hud import install_adapters
  install_adapters(native=True)
  import main as cluster
  assert callable(cluster.main)
  expected = json.loads((ROOT / 'NEXO_RUNTIME.json').read_text())
  if tensorrt.__version__ != expected['runtime']['tensorrt']:
    raise ValueError('TensorRT version differs from bundle')
  model = ROOT / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  spec = spec_from_onnx(str(model), frame_skip=4).to_dict()
  if spec != expected['model']:
    raise ValueError('NEXO ONNX contract differs from bundle')
  cache = EngineCache(Path(sys.argv[1]), TrtBackend())
  # Candidate preparation must keep the previous release's engine available
  # for immediate rollback even when the ordinary six-plan cache is full.
  cache.prune = lambda *args, **kwargs: None
  host = EngineHost(cache)
  try:
    host.request(Request(spec['sha256'], spec['nbytes'], spec['frame_skip']), None)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
      status = host.status(spec['sha256'], spec['frame_skip'])
      if status['state'] == 'ready':
        break
      if status['state'] not in ('building', 'loading'):
        raise RuntimeError(str(status))
      time.sleep(.2)
    else:
      raise TimeoutError('NEXO engine preparation timed out')
    loaded = host.loaded
    if loaded.spec.to_dict() != spec:
      raise ValueError('Loaded TensorRT engine contract differs from NEXO model')
    loaded.queues.reset()
    for _ in range(3):
      loaded.queues.step_into(np.zeros(loaded.spec.warped_shape, np.uint8),
                             np.zeros(loaded.spec.packed_nelem, np.float32), loaded.host_inputs)
      values = loaded.engine.run()
      if not values or not all(np.isfinite(value).all() for value in values.values()):
        raise ValueError('NEXO TensorRT output is not finite')
    print('NEXO_PROBE_OK', spec['sha256'], flush=True)
  finally:
    host.close()


if __name__ == '__main__':
  main()
