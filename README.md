# Image Review Tool

A small local web app for triaging a bounded sample of classified insect crops per order, so a human reviewer doesn't have to go through the entire dataset (100,000+ crops) to get a real accuracy signal. Just need Python 3.

## What it does

Two dropdowns at the top control what you see:

1. **Class**: Diptera, Hymenoptera, ...
2. **Confidence bag**: *All*, *High* (≥80%), *Medium* (50–80%), or *Low*(<50%). Each option shows how many you've reviewed and how many are available in that bag.

**Features:**
- Sample drawn **one at a time, in random order** from a class + confidence.
- Switch class or bag whenever you like.
- Review card as showing calibration view (it shows whether "high confidence" really means mostly-correct and "low confidence" really means mostly-wrong.)

For each crop shown, we pick:

- **Correct**: the predicted order is right
- **Wrong**: it's misclassified (optionally say what it actually is)
- **Discard**: this isn't a real detection (debris, artifact, tray edge, etc.)

The original data folder is **never modified**. Decisions go to:

```
<out_dir>/
  verified_correct/<Order>/<crop_filename>
  verified_wrong/<Order>/correctedas_<Order>__<crop_filename>   (if specified a correction)
  discard/<Order>/<crop_filename>
  decisions.csv                                                 (alone has everything needed to compute real per-order accuracy)
  review_state.db                                               (progress database, safe to stop/resume)
```

## Running it

```bash
python3 server.py \
  --base_dir /path/to/classification_by_confidence \
  --out_dir  /path/to/review_output \
  --port 8765
```

(Optional: `--per_order N` caps the queue per order, e.g. for a quick test. The default is effectively unlimited, the whole directory.)

Then open **http://localhost:8765** in a browser.

- `--base_dir` should point at a `classification_by_confidence/` folder (subfolders per Order, each with `high_conf/`, `medium_conf/`, `low_conf/`).
- Progress is saved continuously, meaning closing the browser or stopping the server (Ctrl+C) and running it again later resumes exactly where you left off, as long as you use the same `--out_dir`.
- Keyboard shortcuts: `C` correct, `W` wrong (opens the correction dropdown), `D` discard, `U` undo last decision.

