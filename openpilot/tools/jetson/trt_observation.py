"""Carrot CUDA resource recovery, limited to explicit Offroad validation.

Adapted from ajouatom/openpilot baab16bb9666, jetlink TensorRT engine.
The pinned run/enqueue/math and model contract remain inherited unchanged.
Never installed into the normal native_host or modeld backend.

Copyright (c) 2026 Zeph Leggett

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from contextlib import ExitStack
import hashlib
import logging
from pathlib import Path

ENGINE_SHA = '3bda5e88432cceda3af13fb4a83873befceb006b6dd76ce308c64e660e2f9648'
PROFILE = 'carrot-offroad-cuda-recovery-v1'


def _release(function, value):
  try:
    function(value)
  except Exception as error:
    # One failed release must not hide the original failure or skip the rest.
    logging.getLogger(__name__).warning('CUDA cleanup failed: %s', type(error).__name__)
    return f"{getattr(function, '__name__', 'release')}: {type(error).__name__}"
  return None


def verify_cleanup(engine):
  if getattr(engine, 'cleanup_errors', None):
    raise RuntimeError('CUDA resource cleanup failed: ' + ', '.join(engine.cleanup_errors))


def engine_class(vendor):
  """Backport cleanup only; refuse an unknown engine or TensorRT ABI."""
  if (vendor.trt.__version__ != '10.3.0'
      or hashlib.sha256(Path(vendor.__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != ENGINE_SHA):
    raise ValueError('Observation runtime requires the pinned Carrot engine and TensorRT 10.3.0')
  cuda, trt, np = vendor.cudart, vendor.trt, vendor.np

  class ObservationEngine(vendor.TrtEngine):
    safety_profile = PROFILE

    def __init__(self, plan_path, log_severity=trt.Logger.WARNING, device=None):
      self.logger = trt.Logger(log_severity)
      initialize = getattr(trt, 'init_libnvinfer_plugins', None)
      if initialize is not None:
        initialize(self.logger, '')
      if device is not None:
        cuda.set_device(device)
      self.runtime = trt.Runtime(self.logger)
      with open(plan_path, 'rb') as source:
        self.engine = self.runtime.deserialize_cuda_engine(source.read())
      if self.engine is None:
        raise RuntimeError(f'failed to deserialize engine {plan_path}')
      self.context = self.engine.create_execution_context()
      if self.context is None:
        raise RuntimeError('failed to create TensorRT execution context')
      self.bindings, self.inputs, self.outputs = {}, {}, {}
      self.graph_exec, self.stream = None, None
      self.cleanup_errors = []
      # Register each allocation immediately, including a device allocation
      # whose matching pinned-host allocation may fail next.
      with ExitStack() as cleanup:
        self.stream = cuda.stream_create()
        cleanup.callback(_release, cuda.stream_destroy, self.stream)
        for index in range(self.engine.num_io_tensors):
          name = self.engine.get_tensor_name(index)
          shape = tuple(self.engine.get_tensor_shape(name))
          if any(d <= 0 for d in shape):
            raise RuntimeError(f'tensor {name} requires a static positive shape: {shape}')
          dtype = np.dtype(vendor.TRT_TO_NP[self.engine.get_tensor_dtype(name)])
          size = int(np.prod(shape)) * dtype.itemsize
          device_ptr = cuda.malloc(size)
          cleanup.callback(_release, cuda.free, device_ptr)
          host_ptr = cuda.host_alloc(size)
          cleanup.callback(_release, cuda.host_free, host_ptr)
          self.bindings[name] = vendor.Binding(name, shape, dtype, size, int(device_ptr), int(host_ptr),
            vendor._np_from_ptr(int(host_ptr), shape, dtype), self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT)
          if self.context.set_tensor_address(name, int(device_ptr)) is False:
            raise RuntimeError(f'failed to bind TensorRT tensor {name}')
        self.inputs = {n: b for n, b in self.bindings.items() if b.is_input}
        self.outputs = {n: b for n, b in self.bindings.items() if not b.is_input}
        self.last_gpu_us = 0
        cleanup.pop_all()

    def capture_graph(self):
      if self.graph_exec is not None:
        return True
      capturing, graph = False, None
      try:
        cuda.stream_begin_capture(self.stream)
        capturing = True
        self._enqueue()
        capturing = False  # EndCapture also ends an invalidated capture.
        graph = cuda.stream_end_capture(self.stream)
        self.graph_exec = cuda.graph_instantiate(graph)
        return True
      except Exception:
        if capturing:
          try:
            graph = cuda.stream_end_capture(self.stream)
          except Exception:
            pass
        self.graph_exec = None
        return False  # Subsequent real synchronized inference must still pass.
      finally:
        if graph is not None:
          error = _release(cuda.graph_destroy, graph)
          if error:
            self.cleanup_errors.append(error)

    def run_without_graph(self):
      """Isolated Offroad-only baseline: execute the same plan without CUDA Graph replay.

      This method is intentionally absent from the pinned production TrtEngine.
      The caller must copy output views before the next GPU invocation.
      """
      self._enqueue()
      cuda.stream_sync(self.stream)
      return {name: binding.host for name, binding in self.outputs.items()}

    def close(self):
      # Capture/inference failures can leave work in flight. Finish it before
      # freeing buffers, then release graph ownership before its allocations.
      def release(function, resource):
        error = _release(function, resource)
        if error:
          self.cleanup_errors.append(error)
      if self.stream is not None:
        release(cuda.stream_sync, self.stream)
      if self.graph_exec is not None:
        release(cuda.graph_exec_destroy, self.graph_exec)
        self.graph_exec = None
      for binding in self.bindings.values():
        release(cuda.free, binding.device_ptr)
        release(cuda.host_free, binding.host_ptr)
      self.bindings.clear()
      self.inputs.clear()
      self.outputs.clear()
      if self.stream is not None:
        release(cuda.stream_destroy, self.stream)
        self.stream = None

  return ObservationEngine


def backend_class(base):
  from jetlink.server.backends.trt import engine
  checked = engine_class(engine)

  class ObservationBackend(base):
    def load(self, artifact, report=None):
      return checked(str(artifact), device=self.device if self.device else None)

  return ObservationBackend
