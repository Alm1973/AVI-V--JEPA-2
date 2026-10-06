"""Benchmark frozen video encoders on this machine. Step 1 of LEARNING-DESIGN.

The question is narrow: can a self-supervised video encoder produce one latent
per sample fast enough, inside 6 GB, while leaving room for the detector?

Nothing is trained and nothing is stored. Frames come from a synthetic clip by
default so this can run with the camera busy; --camera uses the real one and
still writes no image anywhere.

    python bench_encoder.py                 # all candidates, synthetic input
    python bench_encoder.py --only videomae
"""
import argparse
import gc
import time

CANDIDATES = [
    # (name, hf id, kind). Order is lightest first, so a machine that cannot
    # hold the big one still produces a usable answer.
    ("videomae-base", "MCG-NJU/videomae-base", "videomae"),
    ("vjepa2-vitl", "facebook/vjepa2-vitl-fpc64-256", "vjepa2"),
]


def vram():
    import torch
    if not torch.cuda.is_available():
        return None
    free, total = torch.cuda.mem_get_info()
    return {"free_GB": round(free / 1e9, 2), "total_GB": round(total / 1e9, 2)}


def clip(frames, size, seed=0):
    """A synthetic moving-gradient clip. Real content does not change timing --
    the shapes do -- and this keeps the camera free."""
    import numpy as np
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, (size, size, 3), dtype=np.uint8)
    out = []
    for i in range(frames):
        out.append(np.roll(base, i * 3, axis=1))
    return out


def camera_clip(frames, size):
    import cv2
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    got = []
    try:
        for _ in range(frames):
            ok, f = cap.read()
            if not ok:
                return None
            got.append(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2RGB),
                                  (size, size)))
    finally:
        cap.release()
    return got


def bench(name, model_id, kind, frames, size, runs, use_camera):
    import torch
    from transformers import AutoModel, AutoVideoProcessor
    t0 = time.time()
    try:
        proc = AutoVideoProcessor.from_pretrained(model_id)
    except Exception:
        from transformers import AutoImageProcessor
        proc = AutoImageProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id, torch_dtype=torch.float16)
    model = model.to("cuda").eval()
    load_s = time.time() - t0

    n_frames = 16 if kind == "videomae" else frames
    pix = (camera_clip(n_frames, size) if use_camera
           else clip(n_frames, size))
    if pix is None:
        return {"name": name, "error": "camera unavailable"}
    inputs = proc(pix, return_tensors="pt")
    inputs = {k: (v.to("cuda").half() if v.dtype.is_floating_point
                  else v.to("cuda")) for k, v in inputs.items()}

    with torch.inference_mode():
        for _ in range(3):                       # warm up kernels and cudnn
            model(**inputs)
        torch.cuda.synchronize()
        times = []
        for _ in range(runs):
            s = time.perf_counter()
            out = model(**inputs)
            torch.cuda.synchronize()
            times.append((time.perf_counter() - s) * 1000)

    h = getattr(out, "last_hidden_state", None)
    dim = tuple(h.shape) if h is not None else None
    peak = torch.cuda.max_memory_allocated() / 1e9
    times.sort()
    res = {"name": name, "id": model_id, "load_s": round(load_s, 1),
           "frames": n_frames, "size": size,
           "ms_median": round(times[len(times) // 2], 1),
           "ms_best": round(times[0], 1), "ms_worst": round(times[-1], 1),
           "peak_vram_GB": round(peak, 2), "out_shape": dim,
           "latent_floats": (dim[-1] if dim else None)}
    del model, inputs, out
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=16)
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--only", default=None)
    ap.add_argument("--camera", action="store_true")
    args = ap.parse_args()

    import sys
    import torch
    import transformers
    print(f"python {sys.version.split()[0]}  torch {torch.__version__}  "
          f"transformers {transformers.__version__}")
    print(f"cuda {torch.cuda.is_available()}  "
          f"{torch.cuda.get_device_name(0) if torch.cuda.is_available() else ''}")
    print(f"vram at start: {vram()}")
    if not torch.cuda.is_available():
        print("no CUDA; refusing to report CPU numbers as if they were the "
              "robot's budget")
        return

    for name, model_id, kind in CANDIDATES:
        if args.only and args.only not in name:
            continue
        print(f"\n--- {name} ---")
        try:
            r = bench(name, model_id, kind, args.frames, args.size,
                      args.runs, args.camera)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}"[:400])
            continue
        for k, v in r.items():
            print(f"  {k}: {v}")
    print(f"\nvram at end: {vram()}")


if __name__ == "__main__":
    main()
