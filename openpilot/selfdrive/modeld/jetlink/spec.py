"""Small Jetlink model-contract helper for the pinned Cinque v2 model."""
from __future__ import annotations

import math
from dataclasses import dataclass


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
    return math.prod(self.output_shapes['outputs'])

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
      sha256=d['sha256'], nbytes=int(d['nbytes']), frame_skip=int(d.get('frame_skip', 4)),
      input_shapes={k: tuple(v) for k, v in d['input_shapes'].items()},
      output_shapes={k: tuple(v) for k, v in d['output_shapes'].items()},
      output_slices={k: slice(*v) for k, v in d['output_slices'].items()},
      checkpoint=d.get('checkpoint'),
    )
