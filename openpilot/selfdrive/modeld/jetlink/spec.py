"""Jetlink model contract derived from the NEXO model already in use.

The transport architecture follows Carrot Jetlink, but NEXO never substitutes
Carrot's Cinque model. The exact driving_supercombo.onnx that local modeld loads
is fingerprinted and its compiled metadata is reused as the remote contract.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

NATIVE_MODEL = Path(__file__).resolve().parent.parent / 'models' / 'driving_supercombo.onnx'


@dataclass(frozen=True)
class ModelSpec:
  sha256: str
  nbytes: int
  frame_skip: int
  input_shapes: dict[str, tuple[int, ...]]
  output_shapes: dict[str, tuple[int, ...]]
  output_slices: dict[str, slice]
  checkpoint: str | None = None

  @property
  def warped_shape(self):
    h, w = self.input_shapes['img'][2:4]
    return (2, 6, h, w)

  @property
  def warped_nbytes(self):
    return math.prod(self.warped_shape)

  @property
  def feat_dim(self):
    return math.prod(self.input_shapes['features_buffer'][2:])

  @property
  def packed_shapes(self):
    dp = self.input_shapes['desire_pulse']
    tc = self.input_shapes['traffic_convention']
    at = self.input_shapes['action_t']
    fb = self.input_shapes['features_buffer']
    return {
      'desire': (dp[2],),
      'traffic_convention': tuple(tc),
      'action_t': tuple(at),
      'prev_feat': (fb[0], self.feat_dim),
    }

  @property
  def packed_sizes(self):
    return [math.prod(v) for v in self.packed_shapes.values()]

  @property
  def packed_nelem(self):
    return sum(self.packed_sizes)

  @property
  def packed_nbytes(self):
    return self.packed_nelem * 4

  @property
  def output_nelem(self):
    if self.output_slices:
      return max(int(v.stop) for v in self.output_slices.values())
    if len(self.output_shapes) != 1:
      raise ValueError('cannot infer flattened model output size')
    return math.prod(next(iter(self.output_shapes.values())))

  @property
  def output_nbytes(self):
    return self.output_nelem * 4

  def to_dict(self):
    return {
      'sha256': self.sha256,
      'nbytes': self.nbytes,
      'frame_skip': self.frame_skip,
      'checkpoint': self.checkpoint,
      'input_shapes': {k: list(v) for k, v in self.input_shapes.items()},
      'output_shapes': {k: list(v) for k, v in self.output_shapes.items()},
      'output_slices': {k: [v.start, v.stop] for k, v in self.output_slices.items()},
    }

  @classmethod
  def from_dict(cls, d):
    return cls(
      sha256=d['sha256'], nbytes=int(d['nbytes']), frame_skip=int(d['frame_skip']),
      input_shapes={k: tuple(v) for k, v in d['input_shapes'].items()},
      output_shapes={k: tuple(v) for k, v in d['output_shapes'].items()},
      output_slices={k: slice(*v) for k, v in d['output_slices'].items()},
      checkpoint=d.get('checkpoint'),
    )


def sha256_file(path: Path) -> tuple[str, int]:
  digest = hashlib.sha256()
  nbytes = 0
  with path.open('rb') as f:
    while chunk := f.read(4 << 20):
      digest.update(chunk)
      nbytes += len(chunk)
  return digest.hexdigest(), nbytes


def native_spec(model_state) -> ModelSpec:
  """Build a remote contract from the already-loaded local ModelState."""
  if not NATIVE_MODEL.is_file():
    raise FileNotFoundError(f'native NEXO model is missing: {NATIVE_MODEL}')
  sha, nbytes = sha256_file(NATIVE_MODEL)
  output_nelem = max(int(v.stop) for v in model_state.output_slices.values())
  return ModelSpec(
    sha256=sha,
    nbytes=nbytes,
    frame_skip=int(model_state.frame_skip),
    input_shapes={k: tuple(v) for k, v in model_state.input_shapes.items()},
    output_shapes={'outputs': (1, output_nelem)},
    output_slices=dict(model_state.output_slices),
    checkpoint='NEXOdriveXPlus/native-driving_supercombo',
  )
