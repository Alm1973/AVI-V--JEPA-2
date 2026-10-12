"""AVI's always-on senses: camera -> short text events. docs/REDESIGN.md step 1.

events.py turns per-frame observations into events (pure Python, tested on the
Mac). store.py keeps them in SQLite. assistant/senses.py is the camera loop
that runs the MediaPipe models on the MSI and feeds both.

Nothing here learns about Shaurya from labels he gives. What it learns (the
lights threshold, which "people" are a poster) it learns from its own data.
"""
