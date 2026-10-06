"""Which checkpoints load with every weight present? Numbers, not vibes.

bench_encoder.py timed MCG-NJU/videomae-base happily while transformers was
warning that query/value/key.bias across all 12 layers were MISSING and had
been newly INITIALISED -- i.e. part of the attention was random, not
pretrained. Timing is identical either way (the shapes do not change), so a
benchmark cannot catch this and the embeddings would have been quietly wrong.

This loads each candidate and reports the mismatch explicitly.
"""
import sys

CANDIDATES = [
    ("videomae-base", "MCG-NJU/videomae-base"),
    ("videomae-base-finetuned-kinetics",
     "MCG-NJU/videomae-base-finetuned-kinetics"),
    ("videomae-base-short", "MCG-NJU/videomae-base-short"),
    ("vjepa2-vitl", "facebook/vjepa2-vitl-fpc64-256"),
]


def check(name, model_id):
    import torch
    from transformers import AutoModel
    model, info = AutoModel.from_pretrained(
        model_id, dtype=torch.float16, output_loading_info=True)
    missing = sorted(info.get("missing_keys") or [])
    unexpected = sorted(info.get("unexpected_keys") or [])
    # Decoder / mask-token keys are EXPECTED to be unexpected when loading a
    # pretraining checkpoint into the encoder-only class. Missing keys are the
    # ones that matter: those were randomly initialised.
    benign = [k for k in unexpected
              if k.startswith(("decoder", "encoder_to_decoder")) or
              k in ("mask_token",)]
    del model
    return {"name": name, "id": model_id,
            "missing": len(missing), "missing_sample": missing[:4],
            "unexpected": len(unexpected),
            "unexpected_benign": len(benign),
            "unexpected_other": [k for k in unexpected if k not in benign][:4],
            "clean": len(missing) == 0}


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for name, model_id in CANDIDATES:
        if only and only not in name:
            continue
        print(f"\n--- {name} ---")
        try:
            r = check(name, model_id)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}"[:300])
            continue
        for k, v in r.items():
            print(f"  {k}: {v}")
        print(f"  VERDICT: {'usable' if r['clean'] else 'DO NOT USE -- '
              'has randomly initialised weights'}")


if __name__ == "__main__":
    main()
