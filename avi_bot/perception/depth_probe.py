"""Does V-JEPA's representation actually encode depth?

The question behind the whole JEPA bet. A video encoder can score well on
every self-supervised objective while representing only texture and motion
statistics -- "advanced fast arithmetic", as Shaurya put it about OpenCV. If
AVI's world model is going to be spatial, its latents have to carry where
things ARE, not just what they look like.

That is testable rather than arguable, because `depth.py` produces ranges from
the arm's own motion. Measured ranges are labels. So: fit a linear head from
latent to log-range, cross-validated, and compare against a permutation null.

Three decisions that make the answer trustworthy:

  LINEAR, not an MLP. The claim being tested is that depth is PRESENT in the
  representation, not that some network can be trained to compute it from
  pixels. A deep enough probe recovers depth from almost any representation
  and tells you about the probe.

  log-range, not range. Parallax goes as 1/rho, so range is wildly
  heteroscedastic -- the difference between 4 m and 8 m is a pixel. In log
  space the target is roughly linear in the thing actually measured, and R^2
  stops being dominated by whichever far object happened to be in frame.

  GROUPED folds. Latents 15 seconds apart in a static room are near-duplicates
  (the existing coherence probe measures exactly that: near-in-time cosine
  0.994). Random k-fold puts near-duplicates on both sides of the split and
  reports memorisation as generalisation. Folds are contiguous blocks of time,
  so a fold is tested on a stretch the head never saw.

A pass is R^2 clearly above the null, not merely positive. The null is the
same pipeline on shuffled labels, which absorbs every way the fit could look
good for free -- dimensionality above sample count most of all.
"""
import numpy as np

# Ridge, not plain least squares: 1024 dimensions against a few hundred
# samples is underdetermined, and an unregularised fit interpolates the
# training set and reports a meaningless R^2.
ALPHAS = (1.0, 10.0, 100.0, 1000.0, 10000.0)

MIN_SAMPLES = 40
MIN_FOLDS = 3


class NotEnoughData(Exception):
    pass


def _ridge(X, y, alpha):
    """Closed-form ridge on centred data. Returns (w, b)."""
    mx, my = X.mean(0), y.mean()
    Xc, yc = X - mx, y - my
    n, d = Xc.shape
    if d <= n:
        A = Xc.T @ Xc + alpha * np.eye(d)
        w = np.linalg.solve(A, Xc.T @ yc)
    else:
        # Dual form: cheaper and better conditioned when d > n, which is the
        # normal case here (1024 dims, a few hundred latents).
        K = Xc @ Xc.T + alpha * np.eye(n)
        w = Xc.T @ np.linalg.solve(K, yc)
    return w, my - mx @ w


def _r2(y, pred):
    ss = float(((y - y.mean()) ** 2).sum())
    if ss == 0:
        return 0.0
    return float(1.0 - ((y - pred) ** 2).sum() / ss)


def time_folds(n, k):
    """Contiguous index blocks, so a fold is a stretch of time.

    Random folds would split near-duplicate latents across train and test and
    turn memorisation into an apparent result.
    """
    edges = np.linspace(0, n, k + 1).astype(int)
    return [(np.r_[np.arange(0, a), np.arange(b, n)], np.arange(a, b))
            for a, b in zip(edges[:-1], edges[1:]) if b > a]


def decode(X, ranges, folds=5, alphas=ALPHAS):
    """Cross-validated R^2 for predicting log-range from a latent.

    X: (n, d) latents. ranges: (n,) measured ranges in any consistent unit --
    the log makes the unit a constant offset, which the intercept absorbs.
    """
    X = np.asarray(X, dtype=np.float64)
    r = np.asarray(ranges, dtype=np.float64)
    if X.ndim != 2:
        raise NotEnoughData(f"latents must be 2-D, got shape {X.shape}")
    if len(r) != len(X):
        raise NotEnoughData(f"{len(X)} latents but {len(r)} ranges")
    if len(X) < MIN_SAMPLES:
        raise NotEnoughData(
            f"{len(X)} samples is too few to cross-validate; want "
            f"{MIN_SAMPLES}")
    if (r <= 0).any():
        raise NotEnoughData("a range of zero or less has no logarithm; "
                            "far-field tracks should be dropped, not clamped")
    y = np.log(r)
    folds = max(MIN_FOLDS, min(int(folds), len(X) // 8 or MIN_FOLDS))
    best = None
    for alpha in alphas:
        preds = np.zeros_like(y)
        for tr, te in time_folds(len(X), folds):
            w, b = _ridge(X[tr], y[tr], alpha)
            preds[te] = X[te] @ w + b
        got = _r2(y, preds)
        if best is None or got > best[1]:
            best = (alpha, got, preds)
    alpha, r2, preds = best
    # In the ORIGINAL unit, so the number means something physical. Median
    # rather than mean: one far outlier should not set the headline.
    err = np.abs(np.exp(preds) - r)
    return {"r2": round(r2, 4), "alpha": alpha, "folds": folds,
            "n": int(len(X)), "dim": int(X.shape[1]),
            "median_abs_err": round(float(np.median(err)), 4),
            "range_span": [round(float(r.min()), 3), round(float(r.max()), 3)]}


def against_null(X, ranges, folds=5, draws=50, rng=None):
    """The same pipeline on shuffled labels. This is the only comparison.

    A positive R^2 on its own means nothing with 1024 dimensions and a few
    hundred samples. Shuffling the labels keeps every property of the data
    except the thing being claimed, so whatever R^2 survives the shuffle is
    what the pipeline hands out for free.
    """
    rng = rng or np.random.default_rng(0)
    real = decode(X, ranges, folds=folds)
    null = []
    for _ in range(int(draws)):
        null.append(decode(X, np.asarray(ranges)[rng.permutation(len(X))],
                           folds=folds)["r2"])
    null = np.array(null)
    sd = float(null.std())
    out = dict(real)
    out.update({
        "null_mean": round(float(null.mean()), 4),
        "null_sd": round(sd, 4),
        "null_max": round(float(null.max()), 4),
        "z": round((real["r2"] - float(null.mean())) / sd, 1) if sd else None,
        "beats_null": bool(real["r2"] > null.max()),
    })
    # Deliberately strict. "Above the null's mean" is satisfied by noise half
    # the time; above its maximum over every draw is a claim worth making.
    out["verdict"] = ("DEPTH IS DECODABLE" if out["beats_null"]
                      and real["r2"] > 0.2 else "no better than chance")
    return out
