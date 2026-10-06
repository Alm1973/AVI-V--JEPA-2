"""The VideoMAE key remap, checked without downloading anything."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assistant"))


class Zeros:
    """Stands in for a tensor: only new_zeros and shape are used."""
    def __init__(self, shape, fill=1.0):
        self.shape = shape
        self.fill = fill

    def new_zeros(self, shape):
        return Zeros(shape, fill=0.0)


class Remap(unittest.TestCase):
    def setUp(self):
        import encoders
        self.enc = encoders

    def state(self, layers=2):
        s = {}
        for i in range(layers):
            p = f"videomae.encoder.layer.{i}.attention.attention."
            s[p + "q_bias"] = Zeros((768,))
            s[p + "v_bias"] = Zeros((768,))
            s[f"videomae.encoder.layer.{i}.output.dense.weight"] = Zeros((768, 768))
        s["videomae.embeddings.patch_embeddings.projection.weight"] = Zeros((1,))
        return s

    def test_q_and_v_are_renamed(self):
        out, _ = self.enc.remap_videomae(self.state())
        self.assertIn("encoder.layer.0.attention.attention.query.bias", out)
        self.assertIn("encoder.layer.0.attention.attention.value.bias", out)
        self.assertNotIn("encoder.layer.0.attention.attention.q_bias", out)

    def test_key_bias_is_created_and_zero(self):
        out, notes = self.enc.remap_videomae(self.state())
        k = out["encoder.layer.1.attention.attention.key.bias"]
        self.assertEqual(k.fill, 0.0, "key.bias must be zeros, not a copy")
        self.assertEqual(k.shape, (768,))
        self.assertEqual(len(notes), 2, "one note per layer")

    def test_24_tensors_become_36(self):
        """12 layers: q+v in the file, q+k+v in the model."""
        out, _ = self.enc.remap_videomae(self.state(layers=12))
        biases = [k for k in out if k.endswith(".bias") and "attention" in k]
        self.assertEqual(len(biases), 36)

    def test_the_wrapper_prefix_is_dropped(self):
        out, _ = self.enc.remap_videomae(self.state())
        self.assertIn("embeddings.patch_embeddings.projection.weight", out)
        self.assertFalse(any(k.startswith("videomae.") for k in out))

    def test_an_already_clean_state_is_untouched(self):
        clean = {"encoder.layer.0.output.dense.weight": Zeros((2, 2))}
        out, notes = self.enc.remap_videomae(clean)
        self.assertEqual(list(out), list(clean))
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
