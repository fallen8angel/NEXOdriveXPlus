"""Actual NEXO graph, ONNX reference arithmetic and pinned Carrot build hook."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import onnx
from onnx import TensorProto as T, helper
from onnx.reference import ReferenceEvaluator
import pytest

from openpilot.tools.jetson import onnx_inputs as adapter

MODEL = Path(__file__).resolve().parents[3] / 'selfdrive/modeld/models/driving_supercombo.onnx'
SHA = 'f73a9e535523d5e9acb9e642c64e33d631825dc8ba74123757d107cedd047bb5'


@pytest.fixture
def vendor(monkeypatch):
  root = Path(os.environ.get('CARROT_JETSON', '/missing'))
  if not (root / 'third_party/jetlink/jetlink/onnx_patch.py').is_file():
    pytest.skip('set CARROT_JETSON to the pinned Carrot checkout')
  monkeypatch.syspath_prepend(str(root / 'third_party/jetlink'))
  from jetlink import onnx_patch
  monkeypatch.setattr(onnx_patch, 'patch_file', onnx_patch.patch_file)
  return root, onnx_patch


@pytest.fixture(scope='module')
def actual():
  assert hashlib.sha256(MODEL.read_bytes()).hexdigest() == SHA
  return onnx.load(str(MODEL))


def small(kind='identity'):
  inputs = [helper.make_tensor_value_info(name, T.UINT8, [1, 256]) for name in adapter.IMAGES]
  nodes, info = [], []
  image_names = list(adapter.IMAGES)
  if kind in ('identity', 'chain'):
    for i, name in enumerate(adapter.IMAGES):
      for depth in range(3 if kind == 'chain' else 1):
        output = f'{name}/bridge{depth}'
        nodes.append(helper.make_node('Identity', [image_names[i]], [output], name=output))
        info.append(helper.make_tensor_value_info(output, T.UINT8, [1, 256]))
        image_names[i] = output
  if kind == 'concat':
    nodes.append(helper.make_node('Concat', image_names, ['cat'], axis=1))
    nodes.append(helper.make_node('Identity', ['cat'], ['bridge_cat']))
    info.extend(helper.make_tensor_value_info(name, T.UINT8, [1, 512]) for name in ('cat', 'bridge_cat'))
    nodes.append(helper.make_node('Cast', ['bridge_cat'], ['result'], name='cast', to=T.FLOAT16))
  else:
    for name in image_names:
      nodes.append(helper.make_node('Cast', [name], [name + '/fp16'], name=name + '/cast', to=T.FLOAT16))
      info.append(helper.make_tensor_value_info(name + '/fp16', T.FLOAT16, [1, 256]))
    nodes.append(helper.make_node('Concat', [name + '/fp16' for name in image_names], ['result'], axis=1))
  graph = helper.make_graph(nodes, 'head', inputs, [helper.make_tensor_value_info('result', T.FLOAT16, [1, 512])],
                            value_info=info)
  return helper.make_model(graph, opset_imports=[helper.make_opsetid('', 17)])


@pytest.mark.parametrize('kind', ['direct', 'identity', 'chain', 'concat'])
def test_supported_heads_keep_outputs_and_all_metadata_consistent(kind):
  original = small(kind)
  before = original.SerializeToString()
  result = adapter.adapt(original)
  assert original.SerializeToString() == before
  assert [v.SerializeToString() for v in result.graph.output] == [v.SerializeToString() for v in original.graph.output]
  assert all(v.type.tensor_type.elem_type == T.FLOAT16 for v in (*result.graph.input, *result.graph.value_info))
  onnx.checker.check_model(result, full_check=True)
  feeds = {'img': np.arange(256, dtype=np.uint8).reshape(1, 256),
           'big_img': np.arange(255, -1, -1, dtype=np.uint8).reshape(1, 256)}
  expected = ReferenceEvaluator(original).run(None, feeds)[0]
  observed = ReferenceEvaluator(result).run(None, {k: v.astype(np.float16) for k, v in feeds.items()})[0]
  np.testing.assert_array_equal(observed.view(np.uint16), expected.view(np.uint16))


@pytest.mark.parametrize('bad', ['float32', 'missing_cast', 'side_branch', 'raw_output', 'mixed_concat', 'stale_info', 'mixed_inputs'])
def test_unsafe_or_incomplete_heads_fail_without_mutating_source(bad):
  model = small()
  cast = next(n for n in model.graph.node if n.op_type == 'Cast')
  if bad == 'float32':
    cast.attribute[0].i = T.FLOAT
  elif bad == 'missing_cast':
    cast.op_type = 'Identity'
    del cast.attribute[:]
  elif bad == 'side_branch':
    model.graph.node.append(helper.make_node('Neg', ['img'], ['unsafe_branch']))
  elif bad == 'raw_output':
    model.graph.output.append(helper.make_tensor_value_info('img', T.UINT8, [1, 256]))
  elif bad == 'mixed_concat':
    model.graph.input.append(helper.make_tensor_value_info('other', T.UINT8, [1, 256]))
    bridge = model.graph.node[0]
    bridge.op_type = 'Concat'
    bridge.input.append('other')
    bridge.attribute.append(helper.make_attribute('axis', 0))
  elif bad == 'stale_info':
    model.graph.value_info[0].type.tensor_type.elem_type = T.FLOAT
  elif bad == 'mixed_inputs':
    model.graph.input[0].type.tensor_type.elem_type = T.FLOAT16
  before = model.SerializeToString()
  with pytest.raises((ValueError, onnx.checker.ValidationError, onnx.shape_inference.InferenceError)):
    adapter.adapt(model)
  assert model.SerializeToString() == before


def test_cast_graph_output_name_is_preserved():
  model = small('concat')
  result = adapter.adapt(model)
  assert result.graph.node[-1].op_type == 'Identity'
  assert result.graph.node[-1].output[0] == result.graph.output[0].name == 'result'


def test_actual_pinned_failure_is_reproduced_then_fixed(vendor, actual):
  _, upstream = vendor
  with pytest.raises(ValueError, match='could not find the head Cast'):
    upstream.patch_uint8_inputs(copy.deepcopy(actual))
  result = adapter.adapt(actual)
  raw, _, casts = adapter.head_plan(actual)
  assert raw == {'img', 'big_img', 'vision/img', 'vision/big_img'}
  assert [actual.graph.node[i].name for i in casts] == ['vision/node__to_copy', 'vision/node__to_copy_1']
  assert all(result.graph.node[i].op_type == 'Identity' for i in casts)
  # Every other operation, all weights, outputs and non-image input contracts remain exact.
  assert [n.SerializeToString() for i, n in enumerate(result.graph.node) if i not in casts] == [
    n.SerializeToString() for i, n in enumerate(actual.graph.node) if i not in casts]
  assert [t.SerializeToString() for t in result.graph.initializer] == [t.SerializeToString() for t in actual.graph.initializer]
  assert [v.SerializeToString() for v in result.graph.output] == [v.SerializeToString() for v in actual.graph.output]
  assert [v.SerializeToString() for v in result.graph.input if v.name not in adapter.IMAGES] == [
    v.SerializeToString() for v in actual.graph.input if v.name not in adapter.IMAGES]
  inferred = onnx.shape_inference.infer_shapes(result, check_type=True, strict_mode=True)
  info = {v.name: v.type.tensor_type.elem_type for v in (*inferred.graph.input, *inferred.graph.value_info)}
  assert all(info[name] == T.FLOAT16 for name in raw)
  assert hashlib.sha256(MODEL.read_bytes()).hexdigest() == SHA


def test_actual_images_through_concat_and_normalization_are_bit_exact(vendor, actual):
  from onnx.utils import Extractor
  from jetlink.queues import RingQueue
  result = adapter.adapt(actual)
  # Actual graph/weights through the first normalization (before Conv), not a
  # synthetic imitation of NEXO's image preprocessing.
  before = Extractor(onnx.shape_inference.infer_shapes(actual)).extract_model(list(adapter.IMAGES), ['vision/div'])
  after = Extractor(onnx.shape_inference.infer_shapes(result)).extract_model(list(adapter.IMAGES), ['vision/div'])
  size = 12 * 128 * 256
  feeds = {'img': (np.arange(size) % 256).astype(np.uint8).reshape(1, 12, 128, 256),
           'big_img': ((255 - np.arange(size)) % 256).astype(np.uint8).reshape(1, 12, 128, 256)}
  # Exercise the pinned server's actual UINT8 -> FP16 LUT buffer path.
  converted = {}
  for name, value in feeds.items():
    queue = RingQueue(value.shape, np.float16)
    queue.push(value[0])
    converted[name] = queue.logical()
  expected = ReferenceEvaluator(before).run(None, feeds)[0]
  observed = ReferenceEvaluator(after).run(None, converted)[0]
  assert np.isfinite(expected).all()
  np.testing.assert_array_equal(observed.view(np.uint16), expected.view(np.uint16))
  assert float(np.max(np.abs(observed.astype(np.float32) - expected.astype(np.float32)))) == 0


def test_preflight_real_model_and_builder_hook_preserve_file_and_vendor(vendor, tmp_path):
  _, upstream = vendor
  vendor_bytes = Path(upstream.__file__).read_bytes()
  assert adapter.preflight(MODEL, SHA)['uint8_values_checked'] == 256
  with pytest.raises(ValueError, match='SHA'):
    adapter.preflight(MODEL, '0' * 64)
  adapter.install_carrot_patch()
  assert upstream.patch_file is adapter.patch_file
  destination = tmp_path / 'patched.onnx'
  upstream.patch_file(str(MODEL), str(destination))
  onnx.checker.check_model(str(destination), full_check=True)
  assert hashlib.sha256(MODEL.read_bytes()).hexdigest() == SHA
  assert Path(upstream.__file__).read_bytes() == vendor_bytes
  with pytest.raises(ValueError, match='overwrite'):
    upstream.patch_file(str(MODEL), str(MODEL))


def test_pinned_builder_rejects_invalid_graph_before_constructing_trt_builder(vendor, monkeypatch, tmp_path):
  root, _ = vendor
  model = small()
  model.graph.output.append(helper.make_tensor_value_info('img', T.UINT8, [1, 256]))
  source = tmp_path / 'unsafe.onnx'
  onnx.save(model, source)
  class Logger:
    WARNING = 1
    def __init__(self, *_):
      pass
  fake = SimpleNamespace(Logger=Logger, IProgressMonitor=object,
                         Builder=lambda *_: pytest.fail('TensorRT builder reached before ONNX preflight'))
  monkeypatch.setitem(sys.modules, 'tensorrt', fake)
  path = root / 'third_party/jetlink/jetlink/server/backends/trt/build.py'
  spec = importlib.util.spec_from_file_location('checked_pinned_trt_builder', path)
  build = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(build)
  monkeypatch.setattr(build, 'available_bytes', lambda: 8 << 30)
  adapter.install_carrot_patch()
  with pytest.raises(ValueError, match='UINT8 graph outputs'):
    build.build_engine(source, tmp_path / 'never.plan', workspace=256 << 20)
  assert not (tmp_path / 'never.plan').exists()


def test_unknown_vendor_is_rejected(vendor, monkeypatch, tmp_path):
  _, upstream = vendor
  foreign = tmp_path / 'onnx_patch.py'
  foreign.write_text('# different upstream version')
  monkeypatch.setattr(upstream, '__file__', str(foreign))
  with pytest.raises(ValueError, match='pinned Carrot'):
    adapter.install_carrot_patch()


def test_native_service_installs_checked_hook_before_server_main(vendor, monkeypatch, tmp_path):
  root, upstream = vendor
  from jetlink.server import main as server
  from openpilot.tools.jetson import native_host
  from openpilot.tools.jetson import trt_diagnostics
  installed = []
  monkeypatch.setattr(trt_diagnostics, 'install', lambda: installed.append(True))
  monkeypatch.setenv('CARROT_JETSON', str(root))
  monkeypatch.setenv('NEXO_JETLINK_TRANSPORT', 'usb')
  monkeypatch.setattr(sys, 'argv', ['server.py', '--transport', 'usb'])
  is_file = Path.is_file
  # Windows normalizes the POSIX marker to backslashes.
  monkeypatch.setattr(Path, 'is_file', lambda p: True if p.as_posix() == '/etc/nv_tegra_release' else is_file(p))
  monkeypatch.setattr(native_host, 'RUNTIME', tmp_path)
  monkeypatch.setattr(server, 'Session', server.Session)
  def checked_main():
    assert upstream.patch_file is adapter.patch_file
    assert installed == [True]
    return 0
  monkeypatch.setattr(server, 'main', checked_main)
  assert native_host.main() == 0
