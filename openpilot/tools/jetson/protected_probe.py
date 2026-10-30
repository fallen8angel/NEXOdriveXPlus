"""Protected Jetson candidate: real host/HUD imports and finite NEXO TensorRT inference."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]


class ProbeReport:
  def __init__(self, path, display_only=False):
    self.path = path
    self.value = {'state': 'running', 'phase': 'imports', 'display_only': display_only,
                  'physical_hud_verified': False, 'synthetic_inference': False}

  def record(self, phase=None, **values):
    if phase is not None:
      self.value['phase'] = phase
    self.value.update(values)
    temporary = self.path.with_suffix('.tmp')
    temporary.write_text(json.dumps(self.value), encoding='utf-8')
    temporary.replace(self.path)
    print('NEXO_PROBE_PHASE', json.dumps(self.value), flush=True)


def validate_outputs(values, shapes):
  import numpy as np
  if (not isinstance(values, dict) or set(values) != set(shapes)
      or any(value.shape != tuple(shapes[name]) or value.dtype != np.float32
             or not np.isfinite(value).all() for name, value in values.items())):
    raise ValueError('NEXO TensorRT output shape/dtype/finite contract failed')


def run_probe(report):
  vendor = ROOT / 'carrot-server'
  sys.path[:0] = [str(ROOT), str(vendor / 'tools/jetlink'), str(vendor / 'third_party/jetlink')]
  os.environ['CARROT_JETSON'] = str(vendor)
  report.record('hud_imports')
  sys.path.insert(0, str(ROOT / 'openpilot/selfdrive/carrot/cluster'))
  from openpilot.tools.jetson.hud import install_adapters
  install_adapters(native=True)
  import main as cluster
  assert callable(cluster.main)
  from display_owner import panel_owner
  assert callable(panel_owner)
  report.record('hud_imports_passed')
  if report.value['display_only']:
    from openpilot.tools.jetson.ncm_display import DisplaySession
    from openpilot.tools.jetson.ncm_server import transport_class
    from hud_navi import HostForwarder
    assert callable(DisplaySession) and callable(HostForwarder) and callable(transport_class())
    report.record('display_software_checked', state='passed')
    return  # Explicit display-only mode never imports/builds/loads a GPU engine.
  report.record('tensorrt_imports')
  import numpy as np
  import tensorrt
  from jetlink.spec import spec_from_onnx
  from openpilot.tools.jetson.trt_diagnostics import install as install_trt_profile
  TrtBackend = install_trt_profile()
  from jetlink.server.cache import EngineCache
  from jetlink.server.session import EngineHost, Request
  from openpilot.tools.jetson.native_host import session_class
  module_spec = importlib.util.spec_from_file_location('nexo_carrot_server', vendor / 'tools/jetlink/server.py')
  module = importlib.util.module_from_spec(module_spec)
  module_spec.loader.exec_module(module)
  assert issubclass(session_class(module.CarrotSession), module.CarrotSession)
  expected = json.loads((ROOT / 'NEXO_RUNTIME.json').read_text())
  if tensorrt.__version__ != expected['runtime']['tensorrt']:
    raise ValueError('TensorRT version differs from bundle')
  model = ROOT / 'openpilot/selfdrive/modeld/models/driving_supercombo.onnx'
  report.record('onnx_contract')
  spec = spec_from_onnx(str(model), frame_skip=4).to_dict()
  if spec != expected['model']:
    raise ValueError('NEXO ONNX contract differs from bundle')
  from openpilot.tools.jetson.onnx_inputs import install_carrot_patch, preflight
  install_carrot_patch()
  print('NEXO_ONNX_PREFLIGHT_OK', json.dumps(preflight(model, spec['sha256'])), flush=True)
  report.record('engine_build_or_load')
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
    report.record('engine_output_contract')
    if loaded.spec.to_dict() != spec:
      raise ValueError('Loaded TensorRT engine contract differs from NEXO model')
    loaded.queues.reset()
    for _ in range(3):
      loaded.queues.step_into(np.zeros(loaded.spec.warped_shape, np.uint8),
                             np.zeros(loaded.spec.packed_nelem, np.float32), loaded.host_inputs)
      values = loaded.engine.run()
      validate_outputs(values, spec['output_shapes'])
    print('NEXO_PROBE_OK', spec['sha256'], flush=True)
    report.record('engine_output_contract', state='passed', synthetic_inference=True)
  finally:
    host.close()


def software_probe(report, system_tools=True):
  """Use the target venv and staged root, without selecting it or claiming USB."""
  vendor = ROOT / 'carrot-server'
  sys.path[:0] = [str(ROOT), str(ROOT / 'openpilot/selfdrive/carrot/cluster'),
                  str(vendor / 'tools/jetlink'), str(vendor / 'third_party/jetlink')]
  os.environ['CARROT_JETSON'] = str(vendor)
  from openpilot.tools.jetson.runtime_audit import audit_tree
  audit = audit_tree(ROOT)
  report.record('source_audit', audit={k: v for k, v in audit.items() if k != 'imports'})
  from openpilot.tools.jetson import boot_display
  report.record('boot_imports')
  boot_display.prepare()
  from openpilot.tools.jetson import hud, hud_encoder, hud_tx, native_host, ncm_display, ncm_server, display_owner
  hud.install_adapters(native=True)
  import main as cluster
  from hud_navi import HostForwarder
  import openpilot.common.usbgpu_bus_lock as bus
  # A developer checkout or old release on PYTHONPATH must not hide omissions.
  for module in (boot_display, hud, hud_encoder, hud_tx, native_host, ncm_display, ncm_server, display_owner, bus, cluster):
    if not Path(module.__file__).resolve().is_relative_to(ROOT):
      raise ImportError(f'Runtime module outside candidate: {module.__name__}: {module.__file__}')
  assert callable(cluster.main) and callable(HostForwarder) and callable(ncm_server.transport_class())
  # Import deferred panel and media dependencies too; no USB device is opened.
  import usb.core
  import usb.backend.libusb1
  import serial
  import av
  import cluster_usb_display
  sys.path.insert(0, str(cluster_usb_display.VENDOR_ROOT))
  from library.lcd import lcd_comm_turing_usb
  assert callable(lcd_comm_turing_usb.send_jpeg) and callable(lcd_comm_turing_usb.send_image)
  from PIL import Image, ImageFont
  ImageFont.truetype(str(ROOT / 'openpilot/selfdrive/assets/fonts/KaiGenGothicKR-Bold.ttf'), 20)
  assert callable(Image.new) and callable(av.open) and callable(serial.Serial) and callable(usb.core.find)
  if system_tools:
    report.record('host_libraries')
    if usb.backend.libusb1.get_backend() is None:
      raise ImportError('libusb-1.0 backend unavailable')
    encoders = subprocess.check_output(['ffmpeg', '-hide_banner', '-encoders'], text=True, stderr=subprocess.STDOUT, timeout=15)
    if 'libx264' not in encoders:
      raise RuntimeError('ffmpeg libx264 encoder unavailable')
  target = system_tools and sys.platform == 'linux' and os.uname().machine == 'aarch64' and sys.version_info[:2] == (3, 10)
  report.record('software_preflight_passed', state='passed', runtime_imports_verified=True, target_imports_verified=target,
                physical_hud_verified=False, graphics_context_verified=False)


def main():
  report = ProbeReport(Path(sys.argv[1]) / 'nexo-probe.json', '--display-only' in sys.argv[2:])
  report.record()
  try:
    if '--software-only' in sys.argv[2:]:
      software_probe(report, system_tools='--development-imports' not in sys.argv[2:])
    else:
      run_probe(report)
  except BaseException as error:
    report.record(state='failed', error=f'{type(error).__name__}: {error}'[:2000])
    raise


if __name__ == '__main__':
  main()
