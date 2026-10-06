"""Does V-JEPA's latent space carry WHERE THE CAMERA WAS LOOKING?

The sweep gives labelled latents: one per level of the hemisphere, each tagged
with its elevation. The question is whether latents from nearby views sit near
each other in V-JEPA's space for reasons that are about the view.

That is a statement about two distance matrices, so the test is a MANTEL test:
rank-correlate the latent distances with the angular distances across every
pair, and compare against the same statistic with the elevation labels
shuffled. Pairs share members and are not independent, which is exactly why a
permutation null over labels is the right one and a textbook p-value is not.

THE CONFOUND IS THE WHOLE DIFFICULTY. Neighbouring elevations in a sweep are
also neighbouring in time, and often in brightness. Slow drift -- light,
auto-exposure, a sensor warming up -- makes temporally close latents similar,
and in a descending sweep that is indistinguishable from encoding elevation.
So the statistic that counts is the PARTIAL Mantel: both distance matrices are
rank-transformed and residualised on |dt| and |dlight| before correlating.
Whatever survives is not time and not brightness.

If elevation and time are themselves nearly perfectly correlated -- a plain
top-to-bottom sweep -- the partial test cannot separate them and the module
says so rather than reporting either answer. order="shuffle" in sweep_dome
exists to prevent that.

One number is reported that a person can read without statistics:
`nn_adjacent`, the fraction of levels whose nearest latent is the
neighbouring elevation, against what chance gives.
"""
import numpy as np

MIN_LEVELS = 10


class NotEnoughData(Exception):
    pass


def _rank(x):
    """Average ranks, ties shared. Spearman on a vector of distances."""
    x = np.asarray(x, dtype=np.float64)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty_like(x)
    ranks[order] = np.arange(len(x), dtype=np.float64)
    # Average tied ranks so repeated distances do not get arbitrary order.
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    return (sums / counts)[inv]


def _upper(m):
    i, j = np.triu_indices(m.shape[0], k=1)
    return m[i, j]


def cosine_distances(X):
    X = np.asarray(X, dtype=np.float64)
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    U = X / n
    return 1.0 - U @ U.T


def _resid(y, covars):
    """y minus its least-squares fit on the covariates (plus intercept)."""
    if not covars:
        return y - y.mean()
    A = np.column_stack([np.ones_like(y)] + covars)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return y - A @ coef


def _corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return 0.0 if den == 0 else float((a * b).sum() / den)


def _stat(dl_rank, elev, covar_ranks):
    de = _rank(_upper(np.abs(elev[:, None] - elev[None, :])))
    return _corr(_resid(dl_rank, covar_ranks), _resid(de, covar_ranks))


def mantel(latents, elevation, t=None, light=None, draws=999, rng=None,
           distances=None):
    """Partial Mantel r between latent distance and elevation distance.

    Returns raw r (no controls), partial r (controlling for whichever of t
    and light are given), a permutation null for the partial, and a verdict.

    `distances` replaces `latents` with a precomputed n x n matrix. It exists
    for the BASELINE: the same test run on raw-pixel distances. Adjacent tilts
    see overlapping parts of the room, so any representation of appearance --
    a thumbnail, even -- is expected to look view-structured. A latent space
    only shows something beyond appearance if it beats that.
    """
    el = np.asarray(elevation, dtype=np.float64)
    n = len(el)
    if distances is not None:
        Dm = np.asarray(distances, dtype=np.float64)
        if Dm.shape != (n, n):
            raise NotEnoughData(f"distance matrix {Dm.shape} for {n} levels")
    else:
        X = np.asarray(latents, dtype=np.float64)
        if X.ndim != 2 or len(X) != n:
            raise NotEnoughData(f"{len(X)} latents against {n} elevations")
        Dm = cosine_distances(X)
    if n < MIN_LEVELS:
        raise NotEnoughData(f"{n} levels; a distance test wants {MIN_LEVELS}")
    rng = rng or np.random.default_rng(0)

    dl = _rank(_upper(Dm))
    covars, named = [], []
    for name, v in (("time", t), ("light", light)):
        if v is not None:
            v = np.asarray(v, dtype=np.float64)
            covars.append(_rank(_upper(np.abs(v[:, None] - v[None, :]))))
            named.append(name)

    raw = _stat(dl, el, [])
    part = _stat(dl, el, covars)
    null = np.array([_stat(dl, el[rng.permutation(n)], covars)
                     for _ in range(int(draws))])
    out = {"levels": n, "controls": named,
           "mantel_r": round(raw, 4), "partial_r": round(part, 4),
           "null_mean": round(float(null.mean()), 4),
           "null_max": round(float(null.max()), 4),
           "p": round(float((1 + (null >= part).sum()) / (1 + len(null))), 4)}

    # Can the controls even be separated from elevation in this sweep?
    if t is not None:
        r_et = _corr(_rank(el), _rank(np.asarray(t, dtype=np.float64)))
        out["elevation_time_r"] = round(r_et, 4)
        if abs(r_et) > 0.9:
            out["verdict"] = (
                "CONFOUNDED: elevation and capture time correlate at "
                f"{r_et:+.2f}, so no test on this sweep can tell encoding the "
                "view from drifting over time. Re-run with order=shuffle.")
            return out

    if part > out["null_max"] and part > 0.2:
        out["verdict"] = ("ENCODES THE VIEW: latents from nearby elevations "
                          "are nearby in V-JEPA space beyond what "
                          f"{' and '.join(named) or 'nothing'} explain.")
    elif raw > 0.2 and part <= out["null_max"]:
        out["verdict"] = ("ONLY THROUGH THE CONTROLS: the raw structure is "
                          f"carried by {' and '.join(named)}, not by the view.")
    else:
        out["verdict"] = "NO: latent distance does not track view distance."
    return out


def compare(latents, pixel_distances, elevation, t=None, light=None,
            draws=999):
    """V-JEPA against the pixel baseline, on identical controls and null.

    The honest headline. "Encodes the view" on its own may only mean "sees
    overlapping parts of the room at adjacent tilts", which a thumbnail also
    does. What V-JEPA adds is the gap between the two.
    """
    v = mantel(latents, elevation, t=t, light=light, draws=draws)
    b = mantel(None, elevation, t=t, light=light, draws=draws,
               distances=pixel_distances)
    gap = v["partial_r"] - b["partial_r"]
    out = {"vjepa": v, "pixels": b, "gap": round(gap, 4)}
    if b["partial_r"] > b["null_max"] and gap < 0.05:
        out["verdict"] = ("NO BETTER THAN PIXELS: V-JEPA's view structure is "
                          "what raw appearance already has. Adjacent tilts "
                          "overlap; that is all this shows.")
    elif gap >= 0.05 and v["partial_r"] > v["null_max"]:
        out["verdict"] = (f"BEYOND APPEARANCE: V-JEPA is more view-structured "
                          f"than raw pixels by {gap:+.3f} in partial r.")
    else:
        out["verdict"] = "INCONCLUSIVE: neither clearly beats the null."
    return out


def nn_adjacent(latents, elevation, tolerance=1.5):
    """Fraction of levels whose nearest latent is a neighbouring elevation.

    `tolerance` is in units of the median gap between sorted elevations, so
    1.5 means "the next level up or down". Chance is roughly 2/(n-1).
    """
    X = np.asarray(latents, dtype=np.float64)
    el = np.asarray(elevation, dtype=np.float64)
    n = len(el)
    D = cosine_distances(X)
    np.fill_diagonal(D, np.inf)
    gap = float(np.median(np.diff(np.sort(el)))) or 1.0
    hits = sum(abs(el[i] - el[int(np.argmin(D[i]))]) <= tolerance * gap
               for i in range(n))
    return {"nn_adjacent": round(hits / n, 3),
            "chance": round(min(1.0, 2.0 / max(1, n - 1)), 3),
            "levels": n}
