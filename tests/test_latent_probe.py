"""Test the go/no-go test itself, on synthetic data with a known answer.

latent_probe decides whether the whole learning stack is worth building, so
its own arithmetic has to be checked against cases where the right answer is
known in advance. In particular it MUST report no structure on pure noise --
k-means will happily cluster noise, and a tool that cannot say "nothing here"
is useless as a gate.
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assistant"))

import latent_probe as lp                                   # noqa: E402


def drifting(n=600, dim=64, states=5, dwell=40, seed=1):
    """Latents from a room that is in one of a few states at a time."""
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(states, dim)) * 3.0
    X, labels = [], []
    s = 0
    for i in range(n):
        if i % dwell == 0:
            s = int(rng.integers(0, states))
        X.append(centres[s] + rng.normal(size=dim) * 0.6)
        labels.append(s)
    return np.array(X, dtype=np.float32), np.array(labels)


class Coherence(unittest.TestCase):
    def test_noise_shows_no_temporal_structure(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(600, 64)).astype(np.float32)
        ts = np.arange(600) * 15.0
        r = lp.temporal_coherence(X, ts, rng=rng)
        self.assertIsNotNone(r)
        self.assertLess(abs(r["d"]), 0.2,
                        f"claimed structure in pure noise: {r}")

    def test_a_room_with_states_shows_structure(self):
        rng = np.random.default_rng(0)
        X, _ = drifting()
        ts = np.arange(len(X)) * 15.0
        r = lp.temporal_coherence(X, ts, rng=rng)
        self.assertGreater(r["d"], 0.5, f"missed real structure: {r}")

    def test_near_and_far_are_both_measured(self):
        rng = np.random.default_rng(0)
        X, _ = drifting(n=200)
        ts = np.arange(len(X)) * 15.0
        r = lp.temporal_coherence(X, ts, rng=rng)
        self.assertGreater(r["n_near"], 50)
        self.assertGreater(r["n_far"], 50)


class MI(unittest.TestCase):
    def test_mi_is_zero_for_independent_labels(self):
        rng = np.random.default_rng(0)
        a = rng.integers(0, 5, 2000)
        b = rng.integers(0, 4, 2000)
        self.assertLess(lp.mutual_information(a, b), 0.02)

    def test_mi_is_entropy_when_labels_agree(self):
        a = np.repeat(np.arange(4), 50)
        # Identical labelling: MI equals H(a) = 2 bits for 4 equal classes.
        self.assertAlmostEqual(lp.mutual_information(a, a.copy()), 2.0,
                               places=6)

    def test_the_null_catches_a_spurious_result(self):
        """Independent labels must NOT beat the permutation null."""
        rng = np.random.default_rng(0)
        a = rng.integers(0, 6, 400)
        b = rng.integers(0, 3, 400)
        r = lp.mi_against_null(a, b, rng, draws=200)
        self.assertFalse(r["beats_null"], r)

    def test_the_null_passes_a_real_result(self):
        rng = np.random.default_rng(0)
        a = np.repeat(np.arange(6), 60)
        b = a // 2                      # genuinely informative
        r = lp.mi_against_null(a, b, rng, draws=200)
        self.assertTrue(r["beats_null"], r)
        self.assertGreater(r["z"], 5.0, r)


class Clustering(unittest.TestCase):
    def test_kmeans_recovers_known_states(self):
        X, truth = drifting(states=4, dwell=50)
        rng = np.random.default_rng(0)
        lab, _, _ = lp.kmeans(lp.unit(X), 4, rng)
        # Each true state should map mostly onto one cluster.
        r = lp.mi_against_null(lab, truth, rng, draws=100)
        self.assertTrue(r["beats_null"], r)

    def test_unit_handles_a_zero_vector(self):
        X = np.zeros((3, 5), dtype=np.float32)
        X[1, 0] = 2.0
        U = lp.unit(X)
        self.assertTrue(np.isfinite(U).all())
        self.assertAlmostEqual(float(np.linalg.norm(U[1])), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
