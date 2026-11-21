"""True CPU execution checks of synthetic miniature split ONNX models."""
import copy
import pytest

onnx = pytest.importorskip('onnx')
np = pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')
from onnx import TensorProto, helper, numpy_helper
from openpilot.tools.jetson.nexo_n16_partition_plan import inspect_graph
from openpilot.tools.jetson.nexo_n16_partition_extract import extract_in_place
from openpilot.tools.jetson.nexo_n16_partition_cpu_parity import (
  synthetic_feeds, cpu_step, load_arrays, compare_array, compare_cases,
  save_arrays,
)


def small_onnx():
  nodes=[helper.make_node('Add',
                          ['x' if i==0 else 't'+str(i-1), 'w'+str(i)],
                          ['y' if i==3 else 't'+str(i)], name='add'+str(i))
         for i in range(4)]
  weights=[numpy_helper.from_array(np.full((1,4),i+1,np.float32), name='w'+str(i))
           for i in range(4)]
  graph=helper.make_graph(
    nodes, 'small-n16-analogue',
    [helper.make_tensor_value_info('x',TensorProto.FLOAT,[1,4])],
    [helper.make_tensor_value_info('y',TensorProto.FLOAT,[1,4])],
    weights,
    value_info=[helper.make_tensor_value_info('t'+str(i),TensorProto.FLOAT,[1,4])
                for i in range(3)])
  model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',17)])
  model.ir_version=min(model.ir_version,11)
  return model


def test_split_cpu_parity_uses_separate_original_left_right_sessions(tmp_path, monkeypatch):
  from openpilot.tools.jetson import nexo_n16_registry as reg
  import openpilot.tools.jetson.nexo_n16_partition_cpu_parity as parity
  monkeypatch.setattr(reg,'EXPECTED_INPUTS',{'x'})
  monkeypatch.setattr(reg,'EXPECTED_OUTPUTS',{'y'})
  monkeypatch.setattr(parity,'SLICES',{})
  original=small_onnx()
  onnx.save_model(original,tmp_path/'original.onnx')
  cut=next(c for c in inspect_graph(original)['candidates'] if c['cut_node_index']==2)
  for side in ('left','right'):
    part=copy.deepcopy(original)
    extract_in_place(part,cut,side)
    onnx.save_model(part,tmp_path/(side+'.onnx'))
  cpu_step('reference',tmp_path/'original.onnx',tmp_path)
  cpu_step('left',tmp_path/'left.onnx',tmp_path)
  cpu_step('right',tmp_path/'right.onnx',tmp_path)
  receipt=compare_cases(tmp_path,{'y':[1,4]})
  assert receipt['state']=='cpu_parity_passed'
  assert receipt['cpu_numerical_parity_verified'] is True
  assert receipt['gpu_inference_verified'] is False
  assert receipt['vehicle_control_enabled'] is False
  assert len(receipt['cases'])==2


def test_synthetic_feeds_reproducible_and_bounded():
  class Input:
    name='new_img'
    type='tensor(float16)'
    shape=[2,6,128,256]
  a=synthetic_feeds([Input()],1)['new_img']
  b=synthetic_feeds([Input()],1)['new_img']
  np.testing.assert_array_equal(a,b)
  assert a.dtype==np.dtype('float16')
  assert a.min()>=0 and a.max()<=255
  assert np.any(a!=synthetic_feeds([Input()],0)['new_img'])


def test_parity_deviation_rejected():
  source=np.array([[0,1,2]],np.float16)
  assert compare_array(source,np.array([[0,1,2.002]],np.float16),'test')['passed']
  assert not compare_array(source,np.array([[0,1,2.1]],np.float16),'test')['passed']
  with pytest.raises(ValueError,match='shape/dtype'):
    compare_array(source,source.astype(np.float32),'test')


def test_no_untrusted_arrays_or_fabricated_success(tmp_path):
  save_arrays(tmp_path/'inputs-0.npz',{'input':np.array([np.nan],np.float32)})
  with pytest.raises(ValueError,match='Nonfinite'):
    load_arrays(tmp_path/'inputs-0.npz')
  save_arrays(tmp_path/'reference-0.npz',{'wrong':np.array([[0]],np.float32)})
  save_arrays(tmp_path/'left-0.npz',{'t1':np.array([[0]],np.float32)})
  save_arrays(tmp_path/'right-0.npz',{'y':np.array([[0]],np.float32)})
  with pytest.raises(ValueError,match='contract'):
    compare_cases(tmp_path,{'y':[1,1]})
