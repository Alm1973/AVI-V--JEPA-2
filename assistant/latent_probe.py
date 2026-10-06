"""Do the latents see his life at all? The go/no-go for the learning stack.

docs/LEARNING-DESIGN.md step 2. Everything above this layer -- the predictor,
learning progress, the gesture bandit -- assumes these vectors carry real
information about the room. This checks that assumption before any of it gets
written.

THE TRAP THIS IS BUILT TO AVOID
k-means always returns clusters. Feed it pure noise and it will hand back
tidy, balanced, meaningless groups, and a plot of them looks exactly as
convincing as a plot of real structure. So every number here is reported
against a NULL: either shuffled labels or random pairs. A result that does not
beat its null is reported as a failure, however pretty it looks.

Three questions, cheapest and most decisive first:

  1. TEMPORAL COHERENCE. Are samples a minute apart more alike than samples
     hours apart? This needs no labels at all and it is the strongest single
     check: if the representation tracks anything real about the room, time
     adjacency must show up in it. If this fails, nothing else matters.
  2. ROUTINE. Does cluster membership carry information about the hour of day?
     Measured as mutual information against a permutation null.
  3. EVENTS. Does it carry information about change.py firing?

    python latent_probe.py
    python latent_probe.py --k 10 --db latents.db
"""
import argparse
import sqlite3

import numpy as np


def load(path, model=None):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT ts, hour, weekday, changed, light_mean, model, pool, dim, vec"
        " FROM latents ORDER BY ts").fetchall()
    db.close()
    if not rows:
        raise SystemExit("no latents yet")
    models = sorted({r["model"] for r in rows})
    pools = sorted({r["pool"] for r in rows})
    if model is None and len(models) > 1:
        raise SystemExit(f"db mixes models {models}; pass --model to pick one")
    if len(pools) > 1:
        raise SystemExit(f"db mixes poolings {pools}; refusing to compare them")
    rows = [r for r in rows if model is None or r["model"] == model]
    dims = {r["dim"] for r in rows}
    if len(dims) > 1:
        raise SystemExit(f"db mixes dims {dims}")
    X = np.stack([np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
                  for r in rows])
    meta = {k: np.array([r[k] for r in rows])
            for k in ("ts", "hour", "weekday", "changed", "light_mean")}
    return X, meta, models[0] if len(models) == 1 else model


def unit(X):
    n = np.linalg.norm(X, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return X / n


def temporal_coherence(X, ts, near_s=120.0, rng=None, pairs=4000):
    """Mean cosine similarity of near-in-time pairs vs random pairs.

    The null here is random pairs from the same data, so the comparison is
    immune to the usual objection that high-dimensional vectors are all
    similar to each other -- that would raise BOTH numbers.
    """
    rng = rng or np.random.default_rng(0)
    U = unit(X)
    n = len(U)
    if n < 20:
        return None
    near = []
    for _ in range(pairs):
        i = rng.integers(0, n)
        lo = np.searchsorted(ts, ts[i] - near_s)
        hi = np.searchsorted(ts, ts[i] + near_s)
        if hi - lo < 2:
            continue
        j = rng.integers(lo, hi)
        if j == i:
            continue
        near.append(float(U[i] @ U[j]))
    far = []
    for _ in range(pairs):
        i, j = rng.integers(0, n, 2)
        if i == j or abs(ts[i] - ts[j]) <= near_s:
            continue
        far.append(float(U[i] @ U[j]))
    if len(near) < 50 or len(far) < 50:
        return None
    near, far = np.array(near), np.array(far)
    pooled = np.sqrt((near.var() + far.var()) / 2.0)
    # How much the scene varied AT ALL. This has to be reported next to d,
    # because a low d has two completely different causes and they demand
    # opposite responses: either the representation is blind, or nothing
    # happened. Measured 2026-10-05 overnight, far_mean was 0.988 -- random
    # pairs 99% alike across 4.8 hours -- which is a dark empty room, not a
    # bad encoder. Reporting only "weak" invites exactly the wrong conclusion.
    spread = 1.0 - float(far.mean())
    return {"near_mean": round(float(near.mean()), 4),
            "far_mean": round(float(far.mean()), 4),
            "scene_spread": round(spread, 4),
            "static_scene": bool(spread < 0.05),
            "n_near": len(near), "n_far": len(far),
            # Cohen's d: how many standard deviations apart. Reported instead
            # of a p-value because with thousands of pairs everything is
            # "significant" and only the size matters.
            "d": round(float((near.mean() - far.mean()) / pooled), 3)
            if pooled else None}


def within_brightness(X, ts, light, rng, buckets=5):
    """Temporal coherence measured INSIDE narrow brightness bands.

    The reason this test exists: the overnight run scored d = 1.367, a clear
    pass, and the window happened to contain sunrise. A slow monotonic change
    in light level produces exactly that signature -- near-in-time frames
    alike, far-apart frames different -- without the representation knowing
    anything about the room at all. Passing test 1 is therefore consistent
    with an encoder that has learned only to measure brightness.

    Splitting into bands of similar light and re-running within each one
    removes that explanation: if the structure survives when every pair
    being compared is equally lit, it is not the light.
    """
    if len(X) < 100:
        return None
    order = np.argsort(light)
    chunks = np.array_split(order, buckets)
    out = []
    for chunk in chunks:
        if len(chunk) < 60:
            continue
        idx = np.sort(chunk)
        r = temporal_coherence(X[idx], ts[idx], rng=rng, pairs=1500)
        if r is None:
            continue
        lo, hi = float(light[idx].min()), float(light[idx].max())
        out.append({"n": len(idx),
                    "light": (round(lo, 1), round(hi, 1)),
                    "light_width": round(hi - lo, 1),
                    "d": r["d"], "spread": r["scene_spread"],
                    # The per-band static guard. Without it this function
                    # reported d for bands whose scenes never changed, where
                    # d is meaningless by test 1's own criterion -- and then
                    # called those bands "weak", which reads as evidence
                    # against the encoder when it is no evidence at all.
                    "static": bool(r["static_scene"])})
    return out


def kmeans(X, k, rng, iters=60, restarts=4):
    best, best_inertia = None, np.inf
    for _ in range(restarts):
        # k-means++ : spread the seeds, so a restart is not wasted on two
        # centres landing in the same blob.
        centres = [X[rng.integers(0, len(X))]]
        for _ in range(k - 1):
            d = np.min(np.stack([((X - c) ** 2).sum(1) for c in centres]), 0)
            if d.sum() <= 0:
                centres.append(X[rng.integers(0, len(X))])
                continue
            centres.append(X[rng.choice(len(X), p=d / d.sum())])
        C = np.stack(centres)
        lab = None
        for _ in range(iters):
            dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1)
            new = dist.argmin(1)
            if lab is not None and (new == lab).all():
                break
            lab = new
            for j in range(k):
                m = lab == j
                if m.any():
                    C[j] = X[m].mean(0)
        inertia = float(((X - C[lab]) ** 2).sum())
        if inertia < best_inertia:
            best_inertia, best = inertia, (lab.copy(), C.copy())
    return best[0], best[1], best_inertia


def mutual_information(a, b):
    """MI in bits between two integer label arrays."""
    av, ai = np.unique(a, return_inverse=True)
    bv, bi = np.unique(b, return_inverse=True)
    joint = np.zeros((len(av), len(bv)))
    np.add.at(joint, (ai, bi), 1.0)
    joint /= joint.sum()
    pa = joint.sum(1, keepdims=True)
    pb = joint.sum(0, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        term = joint * np.log2(joint / (pa * pb))
    return float(np.nansum(term))


def mi_against_null(labels, target, rng, draws=300):
    """MI, and where it sits in the distribution of MI under shuffling."""
    real = mutual_information(labels, target)
    null = np.array([mutual_information(labels, rng.permutation(target))
                     for _ in range(draws)])
    sd = null.std()
    return {"mi_bits": round(real, 4),
            "null_mean": round(float(null.mean()), 4),
            "null_sd": round(float(sd), 4),
            "z": round(float((real - null.mean()) / sd), 1) if sd else None,
            "beats_null": bool(real > null.max())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="latents.db")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--model", default=None)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    X, meta, model = load(args.db, args.model)
    span_h = (meta["ts"][-1] - meta["ts"][0]) / 3600.0
    print(f"{len(X)} latents, dim {X.shape[1]}, {span_h:.1f} h span")
    print(f"model {model}")
    print(f"changed fired on {int(meta['changed'].sum())} of {len(X)}")

    print("\n1. TEMPORAL COHERENCE  (near-in-time vs random pairs)")
    tc = temporal_coherence(X, meta["ts"], rng=rng)
    if tc is None:
        print("   not enough data yet")
    else:
        for k, v in tc.items():
            print(f"   {k}: {v}")
        d = tc["d"]
        if tc["static_scene"]:
            print(f"   -> INCONCLUSIVE: the scene barely varied "
                  f"(random pairs {tc['far_mean']:.3f} alike, spread "
                  f"{tc['scene_spread']:.3f}). There was nothing to track, so "
                  f"d={d} says nothing about the representation. Re-run over a "
                  f"period with activity in it.")
        else:
            verdict = ("STRUCTURE" if d and d > 0.5 else
                       "weak" if d and d > 0.2 else "NO STRUCTURE")
            print(f"   -> {verdict} (d>0.5 is a clear yes, d<0.2 means the "
                  f"representation is not tracking the room)")

    Xn = unit(X)
    lab, _, inertia = kmeans(Xn, args.k, rng)
    sizes = np.bincount(lab, minlength=args.k)
    print(f"\n   k={args.k} cluster sizes: {sizes.tolist()}")

    print("\n2. ROUTINE  (does cluster carry information about hour of day?)")
    # Within ONE continuous session, hour-of-day IS the time index, so this
    # cannot tell a daily routine from slow drift -- auto-exposure, a warming
    # sensor, light creeping across a wall. It only becomes a statement about
    # routine once the same hours recur on different days.
    days = len({int(t // 86400) for t in meta["ts"]})
    if days < 2:
        print(f"   NOTE: {days} day of data. Hour-of-day is indistinguishable "
              f"from elapsed time here, so a high score below means the "
              f"latents DRIFT, not that a routine was found. Needs 2+ days.")
    # Coarse buckets: 24 hours over a few hundred samples is too sparse to
    # estimate MI, and the question is really about parts of the day.
    bucket = meta["hour"] // 3
    if len(np.unique(bucket)) < 2:
        print("   only one part of the day recorded; come back with more")
    else:
        for k, v in mi_against_null(lab, bucket, rng).items():
            print(f"   {k}: {v}")

    print("\n2b. IS IT JUST THE LIGHT?  (coherence within brightness bands)")
    bands = within_brightness(X, meta["ts"], meta["light_mean"], rng)
    if not bands:
        print("   not enough data per band yet")
    else:
        for b in bands:
            if b["static"]:
                verdict = "INCONCLUSIVE (nothing happened in this band)"
            elif b["d"] and b["d"] > 0.5:
                verdict = "structure"
            elif b["d"] and b["d"] > 0.2:
                verdict = "weak"
            else:
                verdict = "none"
            print(f"   light {b['light'][0]:>6.1f}-{b['light'][1]:<6.1f} "
                  f"(width {b['light_width']:>5.1f})  n={b['n']:<5} "
                  f"d={b['d']:<6} spread={b['spread']:<7} {verdict}")
        usable = [b for b in bands if not b["static"]]
        strong = [b for b in usable if b["d"] and b["d"] > 0.5]
        print(f"   -> {len(usable)} of {len(bands)} bands had anything "
              f"happening in them; {len(strong)} of those kept their "
              f"structure.")
        if not usable:
            print("   VERDICT: this window cannot answer the question. Every "
                  "band is a static scene.")
        else:
            # A band that is wide in brightness has not held brightness
            # constant, so structure inside it is not evidence against the
            # lighting explanation.
            widths = sorted(b["light_width"] for b in usable)
            if strong and min(b["light_width"] for b in strong) > 5.0:
                print("   VERDICT: the only bands with structure are also the "
                      "WIDEST in brightness, so the lighting is not "
                      "controlled for and remains the simplest explanation. "
                      "Needs a window where the light is steady and the room "
                      "is not.")
            elif strong:
                print("   VERDICT: structure survives with brightness roughly "
                      "constant, so test 1 is not measuring the lighting.")
            else:
                print("   VERDICT: no band keeps its structure once brightness "
                      "is held constant. Test 1 measured the lighting.")

    print("\n3. EVENTS  (does cluster carry information about change firing?)")
    if len(np.unique(meta["changed"])) < 2:
        print("   change never varied; nothing to test")
    else:
        for k, v in mi_against_null(lab, meta["changed"], rng).items():
            print(f"   {k}: {v}")

    print("\nGO/NO-GO: test 1 is the one that decides it. Tests 2 and 3 say "
          "whether the structure lines up with anything already measured.")


if __name__ == "__main__":
    main()
