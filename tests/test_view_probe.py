"""The instrument has to say yes, say no, and -- the hard one -- not mistake
drifting over time for encoding the view.

A descending sweep visits elevations in time order, so anything that drifts
slowly makes neighbouring levels similar. These tests build exactly that
situation and require the probe to refuse it.
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
    from avi_bot.perception import view_probe as vp

N, D = 36, 64


def levels(order="shuffle", seed=0):
    el = np.arange(N, dtype=np.float64) * 6.0
    if order == "shuffle":
        el = np.random.default_rng(seed).permutation(el)
    t = np.arange(N, dtype=np.float64) * 5.0          # visit order is time
    return el, t


def view_latents(el, noise=0.15, seed=1):
    """A representation that genuinely varies smoothly with the view."""
    rng = np.random.default_rng(seed)
    basis = rng.normal(size=(3, D))
    phase = np.radians(el)
    feats = np.column_stack([np.sin(phase), np.cos(phase), phase / 3.0])
    return feats @ basis + rng.normal(scale=noise, size=(len(el), D))


def drift_latents(t, noise=0.15, seed=2):
    """A representation that only tracks TIME -- slow drift, no view."""
    rng = np.random.default_rng(seed)
    basis = rng.normal(size=(1, D))
    return (t / t.max())[:, None] * basis * 3.0 + rng.normal(
        scale=noise, size=(len(t), D))


@unittest.skipIf(np is None, "needs numpy")
class ItFindsTheView(unittest.TestCase):
    def test_a_view_encoding_passes_even_controlling_for_time(self):
        el, t = levels("shuffle")
        got = vp.mantel(view_latents(el), el, t=t, draws=199)
        self.assertIn("ENCODES THE VIEW", got["verdict"], got)
        self.assertGreater(got["partial_r"], got["null_max"])

    def test_nearest_neighbours_are_adjacent_levels(self):
        el, _ = levels("shuffle")
        got = vp.nn_adjacent(view_latents(el), el)
        self.assertGreater(got["nn_adjacent"], 0.7)
        self.assertLess(got["chance"], 0.1)


@unittest.skipIf(np is None, "needs numpy")
class ItRefusesDrift(unittest.TestCase):
    def test_drift_in_a_shuffled_sweep_is_attributed_to_time(self):
        """Shuffling decorrelates elevation from time, so a drift-only
        representation shows no elevation structure once time is held."""
        el, t = levels("shuffle")
        got = vp.mantel(drift_latents(t), el, t=t, draws=199)
        self.assertNotIn("ENCODES THE VIEW", got["verdict"], got)

    def test_a_top_to_bottom_sweep_is_declared_confounded(self):
        """The exact trap. In a descending sweep, drift-only latents produce
        a strong RAW Mantel r -- it would look like a discovery -- and the
        probe must refuse to call it either way."""
        el, t = levels("down")
        got = vp.mantel(drift_latents(t), el, t=t, draws=199)
        # Measured 0.39 here -- well clear of chance, which is what makes it
        # a trap. The companion test below shows it passes as "encodes the
        # view" when the time control is dropped.
        self.assertGreater(got["mantel_r"], 0.3,
                           "the trap should look convincing before controls")
        self.assertIn("CONFOUNDED", got["verdict"])

    def test_without_the_time_control_drift_would_have_passed(self):
        """Pins why the control exists: drop it and a descending sweep of
        pure drift is reported as encoding the view."""
        el, t = levels("down")
        got = vp.mantel(drift_latents(t), el, t=None, draws=199)
        self.assertIn("ENCODES THE VIEW", got["verdict"])

    def test_noise_says_no(self):
        el, t = levels("shuffle")
        X = np.random.default_rng(9).normal(size=(N, D))
        got = vp.mantel(X, el, t=t, draws=199)
        self.assertIn("NO", got["verdict"])


@unittest.skipIf(np is None, "needs numpy")
class Brightness(unittest.TestCase):
    def test_a_light_confound_is_removed_by_the_light_control(self):
        """If brightness follows elevation and the latent follows brightness,
        the view is not what is encoded."""
        el, t = levels("shuffle")
        light = 60.0 + el * 0.8
        rng = np.random.default_rng(4)
        X = (light / light.max())[:, None] * rng.normal(size=(1, D)) * 3.0 \
            + rng.normal(scale=0.15, size=(N, D))
        got = vp.mantel(X, el, t=t, light=light, draws=199)
        self.assertNotIn("ENCODES THE VIEW", got["verdict"], got)


@unittest.skipIf(np is None, "needs numpy")
class Refusals(unittest.TestCase):
    def test_too_few_levels(self):
        with self.assertRaises(vp.NotEnoughData):
            vp.mantel(np.zeros((5, 4)), np.arange(5.0))

    def test_mismatched_lengths(self):
        with self.assertRaises(vp.NotEnoughData):
            vp.mantel(np.zeros((20, 4)), np.arange(19.0))

    def test_ties_share_a_rank(self):
        r = vp._rank(np.array([1.0, 2.0, 2.0, 3.0]))
        self.assertEqual(list(r), [0.0, 1.5, 1.5, 3.0])


@unittest.skipIf(np is None, "needs numpy")
class AgainstThePixelBaseline(unittest.TestCase):
    """Adjacent tilts see overlapping parts of the room, so ANY representation
    of appearance looks view-structured. V-JEPA only shows something beyond
    appearance if it beats raw pixels on the same test."""

    def pixel_dist(self, X):
        return vp.cosine_distances(X)

    def test_a_latent_that_is_just_the_pixels_does_no_better(self):
        el, t = levels("shuffle")
        X = view_latents(el)
        got = vp.compare(X, self.pixel_dist(X), el, t=t, draws=199)
        self.assertIn("NO BETTER THAN PIXELS", got["verdict"], got)
        self.assertLess(abs(got["gap"]), 0.05)

    def test_a_latent_cleaner_than_the_pixels_is_credited(self):
        el, t = levels("shuffle")
        latent = view_latents(el, noise=0.05, seed=3)
        noisy_pixels = view_latents(el, noise=2.5, seed=4)
        got = vp.compare(latent, self.pixel_dist(noisy_pixels), el, t=t,
                         draws=199)
        self.assertIn("BEYOND APPEARANCE", got["verdict"], got)
        self.assertGreater(got["gap"], 0.05)

    def test_distances_and_latents_paths_agree(self):
        el, t = levels("shuffle")
        X = view_latents(el)
        a = vp.mantel(X, el, t=t, draws=99)
        b = vp.mantel(None, el, t=t, draws=99,
                      distances=vp.cosine_distances(X))
        self.assertAlmostEqual(a["partial_r"], b["partial_r"], places=9)

    def test_a_wrong_sized_matrix_is_refused(self):
        el, _ = levels("shuffle")
        with self.assertRaises(vp.NotEnoughData):
            vp.mantel(None, el, distances=np.zeros((5, 5)))
