"""Can a linear head read depth out of a latent?

The probe is the instrument, so these test the instrument: it must say yes to
a representation that carries depth, say no to one that does not, and -- the
one that matters -- not be fooled by autocorrelation, because latents fifteen
seconds apart in a static room are near-duplicates (the coherence probe
measures near-in-time cosine at 0.994).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None

if np is not None:
    from avi_bot.perception import depth_probe as dp


@unittest.skipIf(np is None, "needs numpy")
class ItFindsDepthWhenDepthIsThere(unittest.TestCase):
    def latents_encoding_depth(self, n=240, d=64, noise=0.3, seed=0):
        rng = np.random.default_rng(seed)
        ranges = np.exp(rng.uniform(np.log(0.3), np.log(5.0), n))
        direction = rng.normal(size=d)
        X = (np.log(ranges)[:, None] * direction
             + rng.normal(scale=noise, size=(n, d)))
        return X, ranges

    def test_it_decodes_a_representation_that_carries_depth(self):
        X, r = self.latents_encoding_depth()
        got = dp.decode(X, r)
        self.assertGreater(got["r2"], 0.8, got)

    def test_it_beats_the_null(self):
        X, r = self.latents_encoding_depth()
        got = dp.against_null(X, r, draws=20)
        self.assertTrue(got["beats_null"], got)
        self.assertEqual(got["verdict"], "DEPTH IS DECODABLE")

    def test_the_error_is_reported_in_the_original_unit(self):
        """log-range is what gets fitted, but metres are what mean something."""
        X, r = self.latents_encoding_depth(noise=0.15)
        got = dp.decode(X, r)
        self.assertLess(got["median_abs_err"], 0.5)
        self.assertEqual(got["range_span"][0], round(float(r.min()), 3))


@unittest.skipIf(np is None, "needs numpy")
class ItSaysNoWhenThereIsNothing(unittest.TestCase):
    def test_pure_noise_does_not_beat_chance(self):
        rng = np.random.default_rng(1)
        X = rng.normal(size=(240, 64))
        r = np.exp(rng.uniform(np.log(0.3), np.log(5.0), 240))
        got = dp.against_null(X, r, draws=20)
        self.assertEqual(got["verdict"], "no better than chance", got)

    def test_high_dimensions_do_not_manufacture_a_result(self):
        """1024 dims against 240 samples is underdetermined. Without ridge and
        without the null this is where a fake result would come from."""
        rng = np.random.default_rng(2)
        X = rng.normal(size=(240, 1024))
        r = np.exp(rng.uniform(np.log(0.3), np.log(5.0), 240))
        got = dp.against_null(X, r, draws=20)
        self.assertFalse(got["beats_null"], got)


@unittest.skipIf(np is None, "needs numpy")
class AutocorrelationDoesNotFoolIt(unittest.TestCase):
    """The important one. Two INDEPENDENT slow random walks share no
    information, but each is locally smooth, so a split that puts a sample's
    own neighbours in the training set can predict it from nothing at all."""

    def two_independent_walks(self, n=240, d=64, seed=3):
        rng = np.random.default_rng(seed)
        X = np.cumsum(rng.normal(scale=1.0, size=(n, d)), axis=0)
        walk = np.cumsum(rng.normal(scale=0.08, size=n))
        ranges = np.exp(walk - walk.mean())      # independent of X
        return X, ranges

    def test_contiguous_folds_report_no_signal(self):
        X, r = self.two_independent_walks()
        got = dp.against_null(X, r, draws=20)
        self.assertEqual(got["verdict"], "no better than chance", got)

    def test_random_folds_would_have_claimed_one(self):
        """Run the same data through random folds to show what the fold design
        is buying. If this ever stops inflating, the fold choice is moot."""
        X, r = self.two_independent_walks()
        y = np.log(r)
        rng = np.random.default_rng(0)
        order = rng.permutation(len(X))
        preds = np.zeros_like(y)
        edges = np.linspace(0, len(X), 6).astype(int)
        for a, b in zip(edges[:-1], edges[1:]):
            te = order[a:b]
            tr = np.setdiff1d(order, te)
            w, bb = dp._ridge(X[tr], y[tr], 10.0)
            preds[te] = X[te] @ w + bb
        shuffled = dp._r2(y, preds)
        honest = dp.decode(X, r)["r2"]
        self.assertGreater(shuffled, honest + 0.2,
                           f"random folds {shuffled:.3f} vs contiguous "
                           f"{honest:.3f}: the leak is not showing")

    def test_folds_are_contiguous_blocks(self):
        folds = dp.time_folds(100, 5)
        self.assertEqual(len(folds), 5)
        for _, te in folds:
            self.assertEqual(list(te), list(range(te[0], te[-1] + 1)))
        seen = np.concatenate([te for _, te in folds])
        self.assertEqual(sorted(seen), list(range(100)))

    def test_train_and_test_never_overlap(self):
        for tr, te in dp.time_folds(97, 4):
            self.assertEqual(len(np.intersect1d(tr, te)), 0)


@unittest.skipIf(np is None, "needs numpy")
class ItRefusesBadInput(unittest.TestCase):
    def test_too_few_samples(self):
        with self.assertRaises(dp.NotEnoughData):
            dp.decode(np.zeros((10, 8)), np.ones(10))

    def test_mismatched_lengths(self):
        with self.assertRaises(dp.NotEnoughData):
            dp.decode(np.zeros((50, 8)), np.ones(49))

    def test_a_nonpositive_range_is_refused_not_clamped(self):
        """Far-field tracks have no range; clamping them to epsilon would put
        a fabricated label in the regression."""
        r = np.ones(50)
        r[7] = 0.0
        with self.assertRaises(dp.NotEnoughData) as cm:
            dp.decode(np.zeros((50, 8)), r)
        self.assertIn("logarithm", str(cm.exception))

    def test_a_one_dimensional_latent_block_is_refused(self):
        with self.assertRaises(dp.NotEnoughData):
            dp.decode(np.zeros(50), np.ones(50))
