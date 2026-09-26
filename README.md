# MassID45 Review Tool

A small local web app for triaging a bounded sample of classified insect
crops per order, so a human reviewer doesn't have to go through the entire
dataset (100,000+ crops) to get a real accuracy signal.

Pure Python standard library — **no pip installs needed**, just Python 3.

## What it does

Pick an order from the dropdown (Diptera, Hymenoptera, ...) and the tool
serves crops from that order **one at a time, in random order**, drawn
from the entire directory (across all high/medium/low confidence tiers).
There is no target to hit — review as many as you can, stop whenever
you've had enough, switch orders whenever you like. Whatever you've
reviewed by then *is* the sample. For each crop shown, you pick:

- **Correct** — the predicted order is right
- **Wrong** — it's misclassified (optionally say what it actually is)
- **Discard** — this isn't a real detection (debris, artifact, tray edge, etc.)

The original data folder is **never modified** — only copied from. Your
decisions go to:

```
<out_dir>/
  verified_correct/<Order>/<crop_filename>
  verified_wrong/<Order>/correctedas_<Order>__<crop_filename>   (if you specified a correction)
  discard/<Order>/<crop_filename>
  decisions.csv           <- send this back, it's the useful bit
  review_state.db         <- progress database, safe to stop/resume
```

## Running it

```bash
python3 server.py \
  --base_dir /path/to/classification_by_confidence \
  --out_dir  /path/to/review_output \
  --port 8765
```

(Optional: `--per_order N` caps the queue per order, e.g. for a quick test.
The default is effectively unlimited — the whole directory.)

Then open **http://localhost:8765** in a browser.

- `--base_dir` should point at a `classification_by_confidence/` folder
  produced by the MassID45 pipeline (subfolders per Order, each with
  `high_conf/`, `medium_conf/`, `low_conf/`).
- Progress is saved continuously — closing the browser or stopping the
  server (Ctrl+C) and running it again later resumes exactly where you
  left off, as long as you use the same `--out_dir`.
- Keyboard shortcuts: `C` correct, `W` wrong (opens the correction dropdown),
  `D` discard, `U` undo last decision.

## Sending results back

Zip up `verified_correct/`, `verified_wrong/`, `discard/`, and
`decisions.csv` (skip `review_state.db`, it's just local progress state)
and send that back. `decisions.csv` alone has everything needed to compute
real per-order accuracy and build a confusion matrix from the "wrong +
corrected" rows.
