"""Regression guards for the opt-in Conv/GELU diagnostic experiment."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "trt_conv_gelu_experiment.py"


class ExperimentGuards(unittest.TestCase):
  def test_syntax(self):
    ast.parse(SOURCE.read_text(encoding="utf-8"))

  def test_requires_signed_model_and_new_destination(self):
    text = SOURCE.read_text(encoding="utf-8")
    self.assertIn('hashlib.sha256(source.read_bytes()).hexdigest() != MODEL_SHA', text)
    self.assertIn('destination.exists() or destination.is_symlink()', text)
    self.assertIn('Refusing to write under the live Jetson runtime', text)

  def test_never_claims_vehicle_validation(self):
    text = SOURCE.read_text(encoding="utf-8")
    self.assertIn('"vehicle_approved": False', text)
    self.assertIn('"numerical_equivalence_verified": False', text)
    self.assertIn('"tensorrt_engine_verified": False', text)


if __name__ == "__main__":
  unittest.main()
