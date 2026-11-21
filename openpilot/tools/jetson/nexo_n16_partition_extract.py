"""Offline-only extraction of SHA-pinned Carrot N16 into two checked ONNX graphs.

No TensorRT import, camera access, driving control, live model or HUD writes.
The parent guarded trial owns the export directory; this module only writes
inside a disposable staging path. Numerical parity is NOT implied.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_graph

# Existing weight-balanced candidate; CPU numerical parity is not yet accepted.
N16_CUT = 364
ALLOWED_N16_CUTS = (360, 361, N16_CUT)
SIDES = ('left', 'right')


def partition_export_path(cache_dir: Path, model_sha256: str, cut: int = N16_CUT) -> Path:
  """Keep alternate CPU-only cut experiments separate from the existing cut-364 export."""
  if type(cut) is not int or cut not in ALLOWED_N16_CUTS:
    raise ValueError('Unsupported N16 partition cut')
  name = 'nexo-n16-partition-export-' + model_sha256[:12]
  if cut != N16_CUT:
    name += '-cut' + str(cut)
  return cache_dir / name


def _node_digest(nodes) -> str:
  digest = hashlib.sha256()
  for node in nodes:
    value = node.SerializeToString()
    digest.update(len(value).to_bytes(8, 'little'))
    digest.update(value)
  return digest.hexdigest()


def _file_digest(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open('rb') as stream:
    for data in iter(lambda: stream.read(4 * 1024 * 1024), b''):
      digest.update(data)
  return digest.hexdigest()


def _initializer_digest(tensor) -> str:
  """Hash initializer identity and exact payload without copying large raw weights."""
  digest = hashlib.sha256()
  digest.update(tensor.name.encode('utf-8'))
  digest.update(int(tensor.data_type).to_bytes(4, 'little', signed=False))
  for dim in tensor.dims:
    digest.update(int(dim).to_bytes(8, 'little', signed=True))
  if tensor.raw_data:
    digest.update(b'raw\\0')
    digest.update(tensor.raw_data)
  else:
    if tensor.external_data:
      raise ValueError('External initializer data was not materialized: ' + tensor.name)
    digest.update(b'proto\\0')
    digest.update(tensor.SerializeToString(deterministic=True))
  return digest.hexdigest()


def _initializer_manifest(initializers) -> dict[str, str]:
  return {tensor.name: _initializer_digest(tensor)
          for tensor in sorted(initializers, key=lambda item: item.name)}


def _initializer_manifest_digest(manifest: dict[str, str]) -> str:
  digest = hashlib.sha256()
  for name, value in sorted(manifest.items()):
    digest.update(name.encode('utf-8'))
    digest.update(bytes.fromhex(value))
  return digest.hexdigest()


def _find_candidate(model, cut: int) -> tuple[dict, dict]:
  plan = inspect_graph(model)
  if plan['state'] != 'candidate_found':
    raise ValueError('No bounded partition candidate: ' + str(plan.get('reason')))
  candidate = next((x for x in plan['candidates'] if x['cut_node_index'] == cut), None)
  if candidate is None:
    raise ValueError('Pinned cut absent from freshly inspected original model')
  return plan, candidate


def _checked_value_info(model, candidate):
  from onnx import helper
  original = {v.name: copy.deepcopy(v) for v in
              [*model.graph.input, *model.graph.value_info, *model.graph.output]}
  for item in candidate['boundary_tensors']:
    name = item['name']
    if name in original:
      if (original[name].type.tensor_type.elem_type != item['dtype']
          or [dim.dim_value for dim in original[name].type.tensor_type.shape.dim] != item['shape']):
        raise ValueError('Boundary tensor type/shape disagreement: ' + name)
    else:
      original[name] = helper.make_tensor_value_info(name, item['dtype'], item['shape'])
  return original


def extract_in_place(model, candidate: dict, side: str) -> dict:
  """Prune nodes and unused initializers; keep original node math and weights."""
  import onnx
  if side not in SIDES:
    raise ValueError('Invalid partition side')
  graph = model.graph
  # Local ONNX FunctionProto definitions are pure model-level operator
  # definitions. Neither graph slicing nor initializer pruning edits them.
  # Keep every original definition and opset import byte-for-byte in BOTH
  # parts. This also permits an original model with unused local functions.
  # Sparse initializers require separate weight accounting and pruning.
  if graph.sparse_initializer:
    raise ValueError('Sparse initializers require separate accounting: count=' +
                     str(len(graph.sparse_initializer)))
  functions_digest_before = _node_digest(model.functions)
  opsets_digest_before = _node_digest(model.opset_import)
  functions_count_before = len(model.functions)
  count = len(graph.node)
  cut = candidate['cut_node_index']
  if not 0 < cut < count:
    raise ValueError('Invalid cut index')
  if len({x.name for x in graph.initializer}) != len(graph.initializer):
    raise ValueError('Duplicate original initializer')
  values = _checked_value_info(model, candidate)
  source_initializer_manifest = _initializer_manifest(graph.initializer)
  origin_inputs = {v.name for v in graph.input}
  origin_outputs = {v.name for v in graph.output}
  input_names = candidate[side + '_inputs']
  output_names = candidate[side + '_outputs']
  required_boundary = {v['name'] for v in candidate['boundary_tensors']}
  if len(input_names) != len(set(input_names)) or len(output_names) != len(set(output_names)):
    raise ValueError('Duplicate planned IO name')
  if side == 'left':
    if not set(input_names) <= origin_inputs or not set(output_names) <= (required_boundary | origin_outputs):
      raise ValueError('Left partition IO contract changed')
    del graph.node[cut:]
  else:
    if set(input_names) != required_boundary | (set(input_names) & origin_inputs):
      raise ValueError('Right partition boundary IO contract changed')
    if not set(output_names) <= origin_outputs:
      raise ValueError('Right partition has non-original outputs')
    del graph.node[:cut]
  used_inputs = {name for node in graph.node for name in node.input if name}
  produced = {name for node in graph.node for name in node.output if name}
  initializers = {v.name for v in graph.initializer}
  used_weights = used_inputs & initializers
  if any(name in initializers for name in input_names + output_names):
    raise ValueError('Initializer is unexpectedly a boundary IO')
  if (used_inputs - used_weights - produced - set(input_names)):
    raise ValueError('Unbound tensor in extracted graph')
  if not set(output_names) <= produced:
    raise ValueError('Output missing in extracted graph')
  # Removing an unused weight never changes any kept arithmetic. Deletion in
  # place avoids holding an extra ~400 MB of initializer copies in Python.
  for index in range(len(graph.initializer) - 1, -1, -1):
    if graph.initializer[index].name not in used_weights:
      del graph.initializer[index]
  retained_initializer_manifest = _initializer_manifest(graph.initializer)
  if any(source_initializer_manifest.get(name) != value
         for name, value in retained_initializer_manifest.items()):
    raise ValueError('Retained initializer data differs from pinned source')
  del graph.input[:]
  graph.input.extend(values[name] for name in input_names)
  del graph.output[:]
  graph.output.extend(values[name] for name in output_names)
  declared = set(input_names) | set(output_names)
  for index in range(len(graph.value_info) - 1, -1, -1):
    name = graph.value_info[index].name
    if name not in used_inputs | produced or name in declared:
      del graph.value_info[index]
  if (_node_digest(model.functions) != functions_digest_before or
      _node_digest(model.opset_import) != opsets_digest_before or
      len(model.functions) != functions_count_before):
    raise ValueError('ONNX local function definitions or opsets changed during extraction')
  # ONNX checker catches undefined values, contradictory graph IO and types.
  onnx.checker.check_model(model, full_check=False)
  return {'side': side, 'cut_node_index': cut, 'original_node_count': count,
          'node_count': len(graph.node), 'node_digest': _node_digest(graph.node),
          'input_names': [v.name for v in graph.input],
          'output_names': [v.name for v in graph.output],
          'retained_initializer_count': len(graph.initializer),
          'source_initializer_count': len(source_initializer_manifest),
          'source_initializer_manifest_sha256': _initializer_manifest_digest(source_initializer_manifest),
          'retained_initializer_manifest': retained_initializer_manifest,
          'retained_initializer_manifest_sha256': _initializer_manifest_digest(retained_initializer_manifest),
          'local_function_count': functions_count_before,
          'local_function_digest': functions_digest_before,
          'opset_import_digest': opsets_digest_before,
          'local_function_definitions_preserved': True,
          'structural_check_passed': True,
          'cpu_numerical_parity_verified': False,
          'gpu_inference_verified': False, 'vehicle_control_enabled': False,
          'hud_prediction_enabled': False}


def write_pinned_half(source: Path, release: Path, destination: Path,
                      side: str, expected_sha256: str, cut: int = N16_CUT) -> dict:
  """Runs inside a short-lived CPU-only process; model memory dies on exit."""
  import sys
  import onnx
  from openpilot.tools.jetson.nexo_n16_trt_trial import patch_stateful_images

  if (source.is_symlink() or not source.is_file() or
      destination.is_symlink() or destination.exists() or
      _file_digest(source) != expected_sha256):
    raise ValueError('Unsafe or unverified N16 source/output')
  sys.path.insert(0, str(release / 'carrot-server/third_party/jetlink'))
  from jetlink.onnx_patch import strip_tinygrad_ops
  model = onnx.load(str(source))
  removed = strip_tinygrad_ops(model)
  patch_stateful_images(model)
  onnx.checker.check_model(model, full_check=False)
  plan, candidate = _find_candidate(model, cut)
  original_digest = _node_digest(model.graph.node)
  if len(model.graph.node) != plan['original_node_count']:
    raise ValueError('Original node count changed during planning')
  receipt = extract_in_place(model, candidate, side)
  destination.parent.mkdir(parents=True, exist_ok=True)
  onnx.save_model(model, str(destination))
  if destination.stat().st_size < 100 or destination.stat().st_size > 900_000_000:
    raise ValueError('Extracted ONNX size outside expected safe bounds')
  receipt.update(source_sha256=expected_sha256,
                 original_node_digest=original_digest,
                 original_node_count=plan['original_node_count'],
                 tinygrad_layout_nodes_removed=removed,
                 partition_file=destination.name,
                 partition_file_bytes=destination.stat().st_size,
                 partition_sha256=_file_digest(destination),
                 boundary_tensors=candidate['boundary_tensors'],
                 model_graph_math_unchanged=True)
  (destination.parent / (side + '.json')).write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
  return receipt


def verify_exported_halves(directory: Path, cut: int = N16_CUT) -> dict:
  """Reload each staged model, require exact shared boundary + node sequence."""
  import onnx
  if directory.is_symlink() or not directory.is_dir():
    raise ValueError('Unsafe partition directory')
  receipts = {}
  node_digest = hashlib.sha256()
  for side in SIDES:
    file = directory / (side + '.onnx')
    receipt_path = directory / (side + '.json')
    if (file.is_symlink() or receipt_path.is_symlink() or
        not file.is_file() or not receipt_path.is_file()):
      raise ValueError('Missing or symlinked partition file')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if (receipt.get('side') != side or receipt.get('cut_node_index') != cut
        or _file_digest(file) != receipt.get('partition_sha256')):
      raise ValueError('Partition file digest or contract mismatch: ' + side)
    model = onnx.load(str(file))
    onnx.checker.check_model(model, full_check=False)
    actual_initializer_manifest = _initializer_manifest(model.graph.initializer)
    if (len(actual_initializer_manifest) != receipt.get('retained_initializer_count')
        or actual_initializer_manifest != receipt.get('retained_initializer_manifest')
        or _initializer_manifest_digest(actual_initializer_manifest) !=
           receipt.get('retained_initializer_manifest_sha256')):
      raise ValueError('Partition initializer data changed: ' + side)
    if ([v.name for v in model.graph.input] != receipt['input_names']
        or [v.name for v in model.graph.output] != receipt['output_names']
        or len(model.graph.node) != receipt['node_count']
        or _node_digest(model.graph.node) != receipt['node_digest']
        or len(model.functions) != receipt['local_function_count']
        or _node_digest(model.functions) != receipt['local_function_digest']
        or _node_digest(model.opset_import) != receipt['opset_import_digest']):
      raise ValueError('Partition contents mutated: ' + side)
    for node in model.graph.node:
      value = node.SerializeToString()
      node_digest.update(len(value).to_bytes(8, 'little'))
      node_digest.update(value)
    receipts[side] = receipt
    del model
  left, right = receipts['left'], receipts['right']
  if (left['original_node_digest'] != right['original_node_digest']
      or left['original_node_count'] != right['original_node_count']
      or left['source_sha256'] != right['source_sha256']
      or left['node_count'] != cut
      or left['node_count'] + right['node_count'] != left['original_node_count']
      or left['boundary_tensors'] != right['boundary_tensors']
      or left['local_function_digest'] != right['local_function_digest']
      or left['opset_import_digest'] != right['opset_import_digest']
      or left['local_function_count'] != right['local_function_count']
      or left['source_initializer_count'] != right['source_initializer_count']
      or left['source_initializer_manifest_sha256'] != right['source_initializer_manifest_sha256']
      or node_digest.hexdigest() != left['original_node_digest']):
    raise ValueError('Recombined exported node stream differs from original')
  boundary = {v['name'] for v in left['boundary_tensors']}
  if not boundary <= set(left['output_names']) or not boundary <= set(right['input_names']):
    raise ValueError('Exported graph boundary is incomplete')
  result = {'state': 'models_extracted', 'phase': 'onnx_partition_extract',
            'cut_node_index': cut, 'original_node_count': left['original_node_count'],
            'left_model_bytes': left['partition_file_bytes'],
            'right_model_bytes': right['partition_file_bytes'],
            'left_sha256': left['partition_sha256'],
            'right_sha256': right['partition_sha256'],
            'boundary_tensor_count': len(boundary),
            'source_sha256': left['source_sha256'],
            'node_stream_recombined_exact': True,
            'local_functions_preserved_identical': True,
            'initializer_integrity_verified': True,
            'source_initializer_count': left['source_initializer_count'],
            'left_retained_initializer_count': left['retained_initializer_count'],
            'right_retained_initializer_count': right['retained_initializer_count'],
            'original_local_function_count': left['local_function_count'],
            'structural_verification_passed': True,
            'numerical_parity_verified': False,
            'gpu_inference_verified': False, 'engine_build_attempted': False,
            'vehicle_control_enabled': False, 'hud_prediction_enabled': False,
            'production_engine_published': False}
  (directory / 'verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
  return result


def half_worker(source: Path, release: Path, directory: Path,
                side: str, expected_sha256: str, cut: int = N16_CUT):
  """Child exit releases the original 800 MB ONNX Python graph before next half."""
  write_pinned_half(source, release, directory / (side + '.onnx'),
                    side, expected_sha256, cut)
