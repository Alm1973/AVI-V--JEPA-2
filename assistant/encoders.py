"""One place that loads frozen video encoders, and refuses to load them wrong.

V-JEPA 2 loads cleanly and is the encoder the robot uses. VideoMAE is the
fallback for when something else wants the VRAM, and it needs a remap first:

    MCG-NJU/videomae-base, transformers 4.x on this machine, reports
      MISSING     encoder.layer.{0..11}.attention.attention.{query,key,value}.bias
      UNEXPECTED  videomae.encoder.layer.{0..11}.attention.attention.{q_bias,v_bias}

Two separate mismatches, and neither is a corrupt download:

  1. the checkpoint keeps the wrapper's `videomae.` prefix, while
     VideoMAEModel's own parameters start at `encoder.`;
  2. VideoMAE stores a q_bias and a v_bias and NO k_bias, because the key
     projection genuinely has no bias in that architecture, while this
     version of the class declares all three as biased Linears.

So 24 real tensors in the file have to become 36 in the model, with the 12
key biases being zeros -- which is not a fudge, it is what the architecture
means. Loading without this leaves every attention bias randomly initialised,
and because the SHAPES are all correct it benchmarks at exactly the same speed
and produces confident garbage. That is why check_weights.py exists.
"""
import re

VJEPA2 = "facebook/vjepa2-vitl-fpc64-256"
VIDEOMAE = "MCG-NJU/videomae-base"


def remap_videomae(state):
    """Checkpoint keys -> VideoMAEModel keys. Returns (new_state, notes)."""
    out, notes = {}, []
    qv = re.compile(r"^(?:videomae\.)?(encoder\.layer\.(\d+)\.attention\."
                    r"attention\.)([qv])_bias$")
    for k, v in state.items():
        m = qv.match(k)
        if m:
            prefix, layer, which = m.group(1), m.group(2), m.group(3)
            name = {"q": "query", "v": "value"}[which]
            out[f"{prefix}{name}.bias"] = v
            if which == "q":
                # The key projection has no bias in VideoMAE. Zero is the
                # architecture's own answer, not a guess.
                out[f"{prefix}key.bias"] = v.new_zeros(v.shape)
                notes.append(f"layer {layer}: key.bias zero-filled")
            continue
        # Drop the wrapper prefix so encoder-only keys line up.
        out[k[len("videomae."):] if k.startswith("videomae.") else k] = v
    return out, notes


def load(model_id=VJEPA2, dtype=None):
    """Load a frozen encoder, raising if any weight would be random.

    The guard is the point. A silently half-initialised encoder is worse than
    a crash, because everything downstream still runs.
    """
    import torch
    from transformers import AutoModel
    dtype = dtype or torch.float16
    model, info = AutoModel.from_pretrained(model_id, dtype=dtype,
                                            output_loading_info=True)
    missing = sorted(info.get("missing_keys") or [])
    if missing and "videomae" in model_id:
        from huggingface_hub import hf_hub_download
        import safetensors.torch as st
        path = hf_hub_download(model_id, "model.safetensors")
        fixed, notes = remap_videomae(st.load_file(path))
        result = model.load_state_dict(
            {k: v.to(dtype) for k, v in fixed.items()
             if k in model.state_dict()}, strict=False)
        missing = sorted(result.missing_keys)
        if notes:
            print(f"[encoders] remapped videomae: {len(notes)} layers")
    if missing:
        raise RuntimeError(
            f"{model_id} loaded with {len(missing)} randomly initialised "
            f"weights, e.g. {missing[:3]}. Refusing to use it: the shapes are "
            f"right so it would benchmark normally and produce garbage.")
    return model.eval()
