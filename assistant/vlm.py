"""One shared Qwen2.5-VL instance for the desk assistant.

Loading matches make_fast_verifier() in avi/run_study.py: 4-bit NF4 through
Transformers, answers read from the first-token scores in one forward pass,
no text generation. The voice loop can import and reuse the same object later,
so the model only sits in GPU memory once.
"""
import math
import random

import cv2

import settings

YES_WORDS = ("Yes", "yes", " Yes", " yes", "YES")
NO_WORDS = ("No", "no", " No", " no", "NO")
LETTERS = "ABCDEFGHIJ"


def softmax(values):
    m = max(values)
    exps = [math.exp(v - m) for v in values]
    s = sum(exps)
    return [e / s for e in exps]


def build_choice_prompt(question, wordings):
    lines = [question]
    for letter, text in zip(LETTERS, wordings):
        lines.append(f"{letter}. {text}")
    lines.append("Answer with the letter only.")
    return "\n".join(lines)


class DeskVLM:
    def __init__(self, model_id=settings.VLM_MODEL,
                 max_pixels=settings.VLM_MAX_PIXELS):
        import torch
        from transformers import (AutoProcessor, AutoModelForImageTextToText,
                                  BitsAndBytesConfig)
        from PIL import Image
        self._torch = torch
        self._Image = Image
        self.proc = AutoProcessor.from_pretrained(model_id, max_pixels=max_pixels)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_id, dtype=torch.float16, device_map="auto",
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4"))
        self.model.eval()

        tok = self.proc.tokenizer

        def first_ids(words):
            out = set()
            for w in words:
                ids = tok.encode(w, add_special_tokens=False)
                if ids:
                    out.add(ids[0])
            return sorted(out)

        self.yes_ids = first_ids(YES_WORDS)
        self.no_ids = first_ids(NO_WORDS)
        self.letter_ids = {L: first_ids([L, " " + L]) for L in LETTERS}
        # A wrong token id silently flips every answer, so refuse to guess.
        if not self.yes_ids or not self.no_ids:
            raise RuntimeError("could not resolve yes/no token ids")
        seen = set()
        for L, ids in self.letter_ids.items():
            if not ids or seen & set(ids):
                raise RuntimeError(f"letter {L} has missing or shared token ids")
            seen |= set(ids)
        self.calls = 0

    def _last_logits(self, prompt, image_bgr):
        img = self._Image.fromarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        msgs = [{"role": "user", "content": [
            {"type": "image", "image": img}, {"type": "text", "text": prompt}]}]
        inp = self.proc.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=True,
            return_dict=True, return_tensors="pt").to(self.model.device)
        with self._torch.inference_mode():
            logits = self.model(**inp).logits[0, -1].float()
        self.calls += 1
        return logits

    def yes_no(self, prompt, image_bgr):
        """Returns (is_yes, margin). Margin = best yes score minus best no score."""
        logits = self._last_logits(prompt, image_bgr)
        y = max(logits[i].item() for i in self.yes_ids)
        n = max(logits[i].item() for i in self.no_ids)
        return y > n, y - n

    def letter_scores(self, prompt, image_bgr, n_options):
        logits = self._last_logits(prompt, image_bgr)
        return [max(logits[i].item() for i in self.letter_ids[L])
                for L in LETTERS[:n_options]]

    def choose(self, question, options, image_bgr, passes=1, rng=None):
        """Multiple choice from first-token letter scores.

        options: list of (label, wording). The order is shuffled on every pass
        and the probabilities are averaged, so a bias toward one letter
        can't decide the answer.
        Returns (best_label, {label: probability}).
        """
        return choose_with(self.letter_scores, question, options, image_bgr,
                           passes=passes, rng=rng)


def choose_with(score_fn, question, options, image_bgr, passes=1, rng=None):
    if len(options) > len(LETTERS):
        raise ValueError(f"at most {len(LETTERS)} options")
    rng = rng or random.Random()
    totals = {label: 0.0 for label, _ in options}
    for _ in range(max(1, passes)):
        order = list(options)
        rng.shuffle(order)
        prompt = build_choice_prompt(question, [w for _, w in order])
        probs = softmax(score_fn(prompt, image_bgr, len(order)))
        for (label, _), p in zip(order, probs):
            totals[label] += p
    n = max(1, passes)
    probs = {label: total / n for label, total in totals.items()}
    best = max(probs, key=probs.get)
    return best, probs
