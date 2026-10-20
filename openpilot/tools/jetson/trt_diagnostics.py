"""Bounded diagnostics around the unmodified, pinned Carrot TensorRT builder.

The reported fused depthwise Conv/GELU failure has no proven root cause yet.
Do not change arithmetic, precision, workspace policy, tactics or cache keys.
No retry/fallback: propagate failure to the existing protected rollback.
"""
from collections import deque
from contextvars import ContextVar
import functools
import hashlib
import json
from pathlib import Path
import subprocess
import time

PROFILE = 'nexo-trt-diagnostics-v1'
MODEL_SHA = 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5'
BUILD_SHA = '35e9a5f57b1a875ce5d8acac2a3e774051351a38fcd3738f99663628a3c6bba8'
_capture = ContextVar('nexo_trt_capture', default=None)
RUNTIME = Path('/opt/carrot-jetlink')


def analyze_model(path):
  """CPU-only description of the exact failing model; never rewrite its graph."""
  import onnx
  from onnx import TensorProto as T, helper

  if hashlib.sha256(Path(path).read_bytes()).hexdigest() != MODEL_SHA:
    raise ValueError('TensorRT diagnostic profile requires the exact NEXO model SHA')
  model = onnx.load(str(path))
  onnx.checker.check_model(model, full_check=True)
  inferred = onnx.shape_inference.infer_shapes(model, check_type=True, strict_mode=True)
  nodes = {n.name: n for n in model.graph.node}
  conv, gelu = nodes['vision/node_conv2d_1'], nodes['vision/node_gelu_1']
  def attrs(node):
    return {a.name: helper.get_attribute_value(a) for a in node.attribute}
  weights = {w.name: w for w in model.graph.initializer}
  values = {v.name: v for v in (*inferred.graph.input, *inferred.graph.value_info, *inferred.graph.output)}
  def tensor(name):
    t = values[name].type.tensor_type
    return {'name': name, 'dtype': T.DataType.Name(t.elem_type), 'shape': [d.dim_value for d in t.shape.dim]}
  return {'profile': PROFILE, 'model_sha256': MODEL_SHA, 'conv': conv.name, 'gelu': gelu.name,
          'group': attrs(conv)['group'], 'kernel': list(weights[conv.input[1]].dims),
          'strides': attrs(conv)['strides'], 'pads': attrs(conv)['pads'],
          'dilations': attrs(conv)['dilations'], 'approximate': attrs(gelu)['approximate'].decode(),
          'weight_dtype': T.DataType.Name(weights[conv.input[1]].data_type),
          'bias_shape': list(weights[conv.input[2]].dims),
          'input': tensor(conv.input[0]), 'boundary': tensor(conv.output[0]), 'output': tensor(gelu.output[0]),
          'graph_inputs': [tensor(v.name) for v in model.graph.input],
          'graph_outputs': [tensor(v.name) for v in model.graph.output],
          'arithmetic_rewrite': False, 'gpu_verified': False, 'root_cause': 'not established'}


def instrument_builder(vendor, trt):
  """Instrument this module's logger only; all CUDA/build calls remain upstream."""
  if trt.__version__ != '10.3.0':
    raise ValueError('NEXO TensorRT diagnostics require TensorRT 10.3.0')
  if hashlib.sha256(Path(vendor.__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != BUILD_SHA:
    raise ValueError('NEXO TensorRT diagnostics require the pinned Carrot builder')
  original = vendor.build_engine
  if getattr(original, 'nexo_diagnostics', None) == PROFILE:
    return

  class Logger(trt.ILogger):
    WARNING = trt.Logger.WARNING

    def __init__(self, severity):
      super().__init__()
      # Capture here: TensorRT can invoke log() on a different native thread.
      self.capture = _capture.get()
      self.fallback = trt.Logger(severity)

    def log(self, severity, message):
      if self.capture is not None:
        self.capture['count'] += 1
        self.capture['tail'].append({'severity': str(severity), 'message': str(message)[:2000]})
        if severity <= trt.Logger.WARNING:
          self.capture['warnings'].append({'severity': str(severity), 'message': str(message)[:2000]})
      # Preserve upstream WARNING console output while collecting VERBOSE.
      self.fallback.log(severity, message)

  class RuntimeView:
    def __getattr__(self, name):
      return getattr(trt, name)
  runtime = RuntimeView()
  runtime.Logger = Logger

  def save(destination, details):
    try:
      # A distinct diagnostic sidecar, never the plan/model or cache metadata.
      temporary = destination.with_suffix('.json.tmp')
      temporary.write_text(json.dumps(details, indent=2), encoding='utf-8')
      temporary.replace(destination)
      print('NEXO_TRT_DIAGNOSTICS_SAVED', destination, flush=True)
    except OSError as exc:
      print(f'NEXO_TRT diagnostics could not be saved: {exc}', flush=True)

  @functools.wraps(original)
  def build(onnx_path, out_path, report=None, fp16=True, optimization_level=3,
            workspace=None, meta_extra=None, timing_cache=None):
    # Exactly the existing sizing policy, sampled once and passed to upstream.
    workspace = vendor.workspace_bytes() if workspace is None else workspace
    events = {'count': 0, 'tail': deque(maxlen=2000), 'warnings': deque(maxlen=200)}
    started = time.monotonic()
    destination = Path(out_path).with_suffix('.build.json')
    details = {'profile': PROFILE, 'state': 'building', 'trt_version': trt.__version__,
               'device': vendor.device_tag(), 'workspace_bytes': workspace,
               'available_bytes': vendor.available_bytes(), 'fp16': fp16,
               'optimization_level': optimization_level, 'tactic_sources': 'unchanged TensorRT defaults',
               'timing_cache': str(timing_cache) if timing_cache is not None else None,
               'attempts': 1, 'arithmetic_rewrite': False, 'gpu_validation_claimed': False}
    token = _capture.set(events)
    try:
      details['model'] = analyze_model(onnx_path)
      print('NEXO_TRT_DIAGNOSTICS', json.dumps(details), flush=True)
      save(destination, details)  # Preserve sizing even if a native crash/OOM bypasses finally.
      result = original(onnx_path, out_path, report=report, fp16=fp16, optimization_level=optimization_level,
                        workspace=workspace, meta_extra=meta_extra, timing_cache=timing_cache)
      details['state'] = 'built'  # Serialization only; protected runtime probe still must pass.
      return result
    except Exception as exc:
      details.update(state='failed', error=f'{type(exc).__name__}: {exc}'[:4000])
      raise
    finally:
      _capture.reset(token)
      details.update(elapsed_seconds=round(time.monotonic() - started, 1),
                     message_count=events['count'], log_tail=list(events['tail']),
                     warning_error_tail=list(events['warnings']),
                     truncated=events['count'] > len(events['tail']))
      save(destination, details)
  build.nexo_diagnostics = PROFILE
  vendor.trt = runtime
  vendor.build_engine = build


def install():
  """NEXO process only; original backend, engine/cache naming and files survive."""
  import tensorrt as trt
  from jetlink.server.backends.trt import TrtBackend, build

  instrument_builder(build, trt)
  return TrtBackend


def collect(output):
  """Standalone stdlib-only snapshot. No installs, service changes or GPU calls."""
  output = Path(output)
  output.mkdir(parents=True, exist_ok=False)
  commands = [
    ['uname', '-a'],
    ['dpkg-query', '-W', '-f=${Package} ${Version}\n', 'nvidia-l4t-core', 'libnvinfer10', 'libnvonnxparsers10'],
    ['systemctl', 'show', 'carrot-jetlink.service', 'carrot-jetlink-hud.service', 'nexo-jetson-ncm.service',
     '-p', 'Id', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result'],
    ['journalctl', '-b', '-u', 'carrot-jetlink', '-u', 'carrot-jetlink-hud', '-n', '300', '--no-pager'],
    ['journalctl', '-k', '-b', '-n', '300', '--no-pager'],
  ]
  report = {'profile': PROFILE, 'read_only_collection': True, 'engine_build_attempts': 0,
            'collected_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'commands': [], 'files': {}}
  for command in commands:
    try:
      result = subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=15, check=False)
      report['commands'].append({'command': command, 'returncode': result.returncode,
                                 'stdout': result.stdout[-262144:], 'stderr': result.stderr[-8192:]})
    except (OSError, subprocess.SubprocessError) as exc:
      report['commands'].append({'command': command, 'error': str(exc)})
  paths = [Path('/proc/meminfo'), Path('/etc/nv_tegra_release'), Path('/run/carrot-storage.json'),
           RUNTIME / 'updates/nexo-install-status.json', RUNTIME / 'updates/nexo-install-transaction.json',
           RUNTIME / 'cache/last-loaded.json']
  paths += sorted((RUNTIME / 'cache/engines').glob(MODEL_SHA[:16] + '.*.build.json'))[:8]
  for path in paths:
    try:
      with path.open(encoding='utf-8', errors='replace') as source:
        report['files'][str(path)] = source.read(6 * 1024 * 1024)
    except OSError as exc:
      report['files'][str(path)] = {'error': str(exc)}
  target = output / 'JETSON_DIAGNOSTICS.json'
  target.write_text(json.dumps(report, indent=2), encoding='utf-8')
  return target


if __name__ == '__main__':
  import argparse
  parser = argparse.ArgumentParser(description='Read-only Jetson logs; no engine build, installation or service changes')
  parser.add_argument('--collect', type=Path, required=True, help='New output directory (must not exist)')
  print(collect(parser.parse_args().collect))
