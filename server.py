#!/usr/bin/env python3
"""
Standalone local review tool for triaging a bounded, stratified sample of
classified insect crops per order. Pure standard library -- no pip installs
needed, just Python 3.

Run:
    python3 server.py --base_dir /path/to/classification_by_confidence \
                       --out_dir  /path/to/review_output \
                       --per_order 100 --port 8765

Then open http://localhost:8765 in a browser.

Produces (under --out_dir, base_dir is never modified):
    verified_correct/<Order>/<crop_filename>
    verified_wrong/<Order>/<crop_filename>       (corrected_order recorded in decisions.csv)
    discard/<Order>/<crop_filename>              (bad detection, not a real insect)
    decisions.csv                                (full log: every decision + metadata)
    review_state.db                              (sqlite progress DB; safe to stop/resume)
"""
import argparse
import csv
import http.server
import json
import os
import random
import re
import shutil
import sqlite3
import socketserver
import sys
import time
import urllib.parse

ORDERS = [
    "Diptera", "Hymenoptera", "Lepidoptera", "Coleoptera", "Hemiptera",
    "Araneae", "Entomobryomorpha", "Psocodea", "Trichoptera", "Blattodea",
    "Opiliones", "Plecoptera", "Neuroptera", "Thysanoptera", "Acari",
    "Poduromorpha", "Symphypleona",
]
TIERS = ["high_conf", "medium_conf", "low_conf"]

CROP_NAME_RE = re.compile(r"^(?P<prob>[01]\.\d+)_(?P<stem>.+)_(?P<idx>\d{4})_score(?P<det>[01]\.\d+)\.png$")


def parse_crop_filename(fname):
    """Parse '<prob>_<source_stem>_<idx>_score<det>.png' -> dict, or None."""
    m = CROP_NAME_RE.match(fname)
    if not m:
        return None
    return {
        "pred_prob": float(m.group("prob")),
        "source_image": m.group("stem") + ".png",
        "det_score": float(m.group("det")),
    }


class ReviewDB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                crop_path TEXT PRIMARY KEY,
                order_name TEXT,
                tier TEXT,
                pred_prob REAL,
                det_score REAL,
                source_image TEXT,
                decision TEXT DEFAULT 'pending',
                corrected_order TEXT,
                reviewed_at REAL
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_tier_decision ON decisions(order_name, tier, decision)"
        )
        self.conn.commit()

    def order_sampled(self, order):
        cur = self.conn.execute("SELECT COUNT(*) FROM decisions WHERE order_name=?", (order,))
        return cur.fetchone()[0] > 0

    def insert_sample(self, rows):
        self.conn.executemany(
            "INSERT OR IGNORE INTO decisions (crop_path, order_name, tier, pred_prob, det_score, source_image) "
            "VALUES (:crop_path, :order_name, :tier, :pred_prob, :det_score, :source_image)",
            rows,
        )
        self.conn.commit()

    def next_pending(self, order, tier=None):
        if tier and tier != "all":
            cur = self.conn.execute(
                "SELECT crop_path, tier, pred_prob, det_score, source_image FROM decisions "
                "WHERE order_name=? AND tier=? AND decision='pending' ORDER BY RANDOM() LIMIT 1",
                (order, tier),
            )
        else:
            cur = self.conn.execute(
                "SELECT crop_path, tier, pred_prob, det_score, source_image FROM decisions "
                "WHERE order_name=? AND decision='pending' ORDER BY RANDOM() LIMIT 1",
                (order,),
            )
        row = cur.fetchone()
        if row is None:
            return None
        return {"crop_path": row[0], "tier": row[1], "pred_prob": row[2], "det_score": row[3], "source_image": row[4]}

    def set_decision(self, crop_path, decision, corrected_order=None):
        ts = None if decision == "pending" else time.time()
        self.conn.execute(
            "UPDATE decisions SET decision=?, corrected_order=?, reviewed_at=? WHERE crop_path=?",
            (decision, corrected_order, ts, crop_path),
        )
        self.conn.commit()

    def last_decision(self, order, tier=None):
        if tier and tier != "all":
            cur = self.conn.execute(
                "SELECT crop_path FROM decisions WHERE order_name=? AND tier=? AND decision!='pending' "
                "ORDER BY reviewed_at DESC LIMIT 1",
                (order, tier),
            )
        else:
            cur = self.conn.execute(
                "SELECT crop_path FROM decisions WHERE order_name=? AND decision!='pending' "
                "ORDER BY reviewed_at DESC LIMIT 1",
                (order,),
            )
        row = cur.fetchone()
        return row[0] if row else None

    def get_row(self, crop_path):
        cur = self.conn.execute(
            "SELECT crop_path, order_name, tier, pred_prob, det_score, source_image, decision, corrected_order "
            "FROM decisions WHERE crop_path=?", (crop_path,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        keys = ["crop_path", "order_name", "tier", "pred_prob", "det_score", "source_image", "decision", "corrected_order"]
        return dict(zip(keys, row))

    def progress(self):
        cur = self.conn.execute(
            "SELECT order_name, tier, decision, COUNT(*) FROM decisions GROUP BY order_name, tier, decision"
        )
        def blank():
            return {"pending": 0, "correct": 0, "wrong": 0, "discard": 0, "total": 0}
        out = {}
        for order, tier, decision, n in cur.fetchall():
            o = out.setdefault(order, {**blank(), "tiers": {}})
            t = o["tiers"].setdefault(tier, blank())
            o[decision] += n
            o["total"] += n
            t[decision] += n
            t["total"] += n
        return out

    def export_csv(self, path):
        cur = self.conn.execute(
            "SELECT crop_path, order_name, tier, pred_prob, det_score, source_image, decision, corrected_order, reviewed_at "
            "FROM decisions ORDER BY order_name, reviewed_at"
        )
        import datetime
        rows = cur.fetchall()
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["crop_path", "predicted_order", "tier", "pred_prob", "det_score", "source_image",
                        "decision", "corrected_order", "reviewed_at"])
            for row in rows:
                row = list(row)
                if row[-1]:
                    row[-1] = datetime.datetime.fromtimestamp(row[-1]).isoformat(timespec="seconds")
                w.writerow(row)


def build_sample(base_dir, order, per_order):
    """Stratified sample across tiers for one order. Returns list of dicts for DB insertion."""
    by_tier = {}
    for tier in TIERS:
        d = os.path.join(base_dir, order, tier)
        files = []
        if os.path.isdir(d):
            for fname in os.listdir(d):
                info = parse_crop_filename(fname)
                if info:
                    files.append((fname, info))
        by_tier[tier] = files

    total_available = sum(len(v) for v in by_tier.values())
    target = min(per_order, total_available)
    if target == 0:
        return []

    # proportional allocation across non-empty tiers, at least 1 each if target allows
    nonempty = [t for t in TIERS if by_tier[t]]
    alloc = {t: 0 for t in TIERS}
    remaining = target
    for t in nonempty:
        share = round(target * len(by_tier[t]) / total_available)
        share = min(share, len(by_tier[t]))
        alloc[t] = share
    # fix rounding drift
    diff = target - sum(alloc.values())
    i = 0
    while diff != 0 and nonempty:
        t = nonempty[i % len(nonempty)]
        if diff > 0 and alloc[t] < len(by_tier[t]):
            alloc[t] += 1
            diff -= 1
        elif diff < 0 and alloc[t] > 0:
            alloc[t] -= 1
            diff += 1
        i += 1
        if i > 10000:
            break

    rows = []
    for tier in TIERS:
        picks = random.sample(by_tier[tier], alloc[tier]) if alloc[tier] else []
        for fname, info in picks:
            rows.append({
                "crop_path": f"{order}/{tier}/{fname}",
                "order_name": order,
                "tier": tier,
                "pred_prob": info["pred_prob"],
                "det_score": info["det_score"],
                "source_image": info["source_image"],
            })
    return rows


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Insect Review Tool</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f7f6f2; color: #211d16; margin: 0; }
  header { background: #211d16; color: #f7f6f2; padding: 14px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  select { font-size: 14px; padding: 6px 10px; border-radius: 6px; border: none; }
  main { max-width: 900px; margin: 24px auto; padding: 0 16px; }
  .progress-bar { height: 8px; background: #ddd8cc; border-radius: 4px; overflow: hidden; margin-bottom: 18px; }
  .progress-fill { height: 100%; background: #256abf; }
  .card { background: white; border: 1px solid #ddd8cc; border-radius: 10px; padding: 20px; text-align: center; }
  .card img { max-width: 100%; max-height: 420px; background: #fff; border: 1px solid #eee; }
  .meta { font-family: monospace; font-size: 13px; color: #5a5346; margin: 14px 0; line-height: 1.7; }
  .meta b { color: #211d16; }
  .btn-row { display: flex; gap: 10px; justify-content: center; margin-top: 16px; flex-wrap: wrap; }
  button { font-size: 15px; padding: 12px 20px; border-radius: 8px; border: none; cursor: pointer; font-weight: 600; }
  .btn-correct { background: #0ca30c; color: white; }
  .btn-wrong { background: #d03b3b; color: white; }
  .btn-discard { background: #898781; color: white; }
  .btn-undo { background: #eee; color: #211d16; }
  .kbd { opacity: 0.7; font-size: 11px; font-weight: 400; display: block; }
  .done { text-align: center; padding: 60px 20px; color: #5a5346; }
  .wrong-panel { margin-top: 14px; display: none; }
  .wrong-panel.show { display: block; }
  .wrong-panel select { width: 260px; }
  .stats-table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 13px; }
  .stats-table td, .stats-table th { padding: 6px 10px; border-bottom: 1px solid #ddd8cc; text-align: right; }
  .stats-table th:first-child, .stats-table td:first-child { text-align: left; }
</style>
</head>
<body>
<header>
  <h1>MassID45 Review Tool</h1>
  <select id="orderSelect" title="Which class to review"></select>
  <select id="tierSelect" title="Which confidence bag to draw from"></select>
  <span id="progressLabel" style="font-family:monospace;font-size:13px;"></span>
</header>
<main>
  <div id="content"></div>
  <h3 id="tierHeading" style="margin:28px 0 4px;font-size:14px;"></h3>
  <table class="stats-table" id="tierTable"></table>
  <h3 style="margin:28px 0 4px;font-size:14px;">All orders</h3>
  <table class="stats-table" id="statsTable"></table>
</main>
<script>
let currentOrder = null;
let currentTier = 'all';
let currentCrop = null;
let orders = [];
const TIER_KEYS = ['all', 'high_conf', 'medium_conf', 'low_conf'];
const TIER_LABELS = {
  all: 'All confidence levels',
  high_conf: 'High confidence (≥80%)',
  medium_conf: 'Medium confidence (50–80%)',
  low_conf: 'Low confidence (<50%)',
};
function pctCorrect(c, w) { const d = c + w; return d ? Math.round(100 * c / d) + '%' : '—'; }

async function api(path, opts) {
  const r = await fetch(path, opts);
  return await r.json();
}

async function loadOrders() {
  const data = await api('/api/orders');
  orders = data.orders;
  const sel = document.getElementById('orderSelect');
  sel.innerHTML = orders.map(o =>
    `<option value="${o.order}">${o.order} — ${o.reviewed} reviewed</option>`
  ).join('');
  renderStats(data.orders);
  if (!currentOrder && orders.length) currentOrder = orders[0].order;
  if (currentOrder) {
    sel.value = currentOrder;
    renderTierSelect();
    renderTierTable();
    loadNext();
  }
}

// The confidence-bag dropdown shows per-bag counts for the currently selected order.
function renderTierSelect() {
  const info = orders.find(o => o.order === currentOrder);
  const sel = document.getElementById('tierSelect');
  sel.innerHTML = TIER_KEYS.map(key => {
    const s = key === 'all' ? info : (info && info.tiers[key]);
    const avail = s ? s.total : 0;
    const rev = s ? s.reviewed : 0;
    return `<option value="${key}">${TIER_LABELS[key]} — ${rev} reviewed, ${avail.toLocaleString()} available</option>`;
  }).join('');
  sel.value = currentTier;
}

function rowHtml(label, s) {
  return `<tr><td>${label}</td><td>${s.total.toLocaleString()}</td><td>${s.reviewed}</td>` +
         `<td>${s.correct}</td><td>${s.wrong}</td><td>${s.discard}</td><td>${pctCorrect(s.correct, s.wrong)}</td></tr>`;
}
const STATS_HEAD = '<tr><th>{first}</th><th>Available</th><th>Reviewed</th><th>Correct</th><th>Wrong</th><th>Discard</th><th>% correct*</th></tr>';

// Selected order broken down by confidence bag -- this is the calibration view:
// does "high confidence" actually mean mostly-correct, and does "low" mean mostly-wrong?
function renderTierTable() {
  const info = orders.find(o => o.order === currentOrder);
  if (!info) return;
  document.getElementById('tierHeading').textContent = currentOrder + ' by confidence bag';
  let html = STATS_HEAD.replace('{first}', 'Confidence bag');
  for (const key of ['high_conf', 'medium_conf', 'low_conf']) {
    const s = info.tiers[key] || {total: 0, reviewed: 0, correct: 0, wrong: 0, discard: 0};
    html += rowHtml(TIER_LABELS[key], s);
  }
  html += rowHtml('<b>All</b>', info);
  html += '<tr><td colspan="7" style="text-align:left;color:#898781;font-size:11px;">* correct / (correct + wrong); discards excluded</td></tr>';
  document.getElementById('tierTable').innerHTML = html;
}

function renderStats(orders) {
  let html = STATS_HEAD.replace('{first}', 'Order');
  for (const o of orders) html += rowHtml(o.order, o);
  document.getElementById('statsTable').innerHTML = html;
}

document.getElementById('orderSelect').addEventListener('change', (e) => {
  currentOrder = e.target.value;
  e.target.blur();
  renderTierSelect();
  renderTierTable();
  loadNext();
});

document.getElementById('tierSelect').addEventListener('change', (e) => {
  currentTier = e.target.value;
  e.target.blur();
  loadNext();
});

async function loadNext() {
  const data = await api('/api/next?order=' + encodeURIComponent(currentOrder) + '&tier=' + encodeURIComponent(currentTier));
  const orderInfo = orders.find(o => o.order === currentOrder);
  if (orderInfo) {
    const s = currentTier === 'all' ? orderInfo : (orderInfo.tiers[currentTier] || {reviewed: 0, correct: 0, wrong: 0, discard: 0});
    document.getElementById('progressLabel').textContent =
      `${currentOrder} · ${TIER_LABELS[currentTier]}: ${s.reviewed} reviewed ` +
      `(${s.correct} correct, ${s.wrong} wrong, ${s.discard} discarded)`;
  }
  const content = document.getElementById('content');
  if (!data.crop) {
    content.innerHTML = `<div class="done">No more crops to review for <b>${currentOrder}</b> in <b>${TIER_LABELS[currentTier]}</b>. Pick another order or confidence level above.</div>`;
    currentCrop = null;
    return;
  }
  currentCrop = data.crop;
  content.innerHTML = `
    <div class="card">
      <img src="/image?path=${encodeURIComponent(currentCrop.crop_path)}">
      <div class="meta">
        <div><b>Predicted:</b> ${currentOrder} (${(currentCrop.pred_prob*100).toFixed(0)}% confidence, ${currentCrop.tier})</div>
        <div><b>Detection score:</b> ${(currentCrop.det_score*100).toFixed(0)}%</div>
        <div><b>Source tray:</b> ${currentCrop.source_image}</div>
      </div>
      <div class="btn-row">
        <button class="btn-correct" onclick="decide('correct')">&#10003; Correct <span class="kbd">C</span></button>
        <button class="btn-wrong" onclick="toggleWrongPanel()">&#10007; Wrong <span class="kbd">W</span></button>
        <button class="btn-discard" onclick="decide('discard')">&#128465; Discard (bad detection) <span class="kbd">D</span></button>
        <button class="btn-undo" onclick="undo()">&#8630; Undo last <span class="kbd">U</span></button>
      </div>
      <div class="wrong-panel" id="wrongPanel">
        <p>What is it actually? (optional, but helps a lot)</p>
        <select id="correctedOrder">
          <option value="">Not sure / skip</option>
          ORDER_OPTIONS
        </select>
        <button class="btn-wrong" onclick="confirmWrong()" style="margin-left:8px;">Confirm</button>
      </div>
    </div>
  `;
}

function toggleWrongPanel() {
  document.getElementById('wrongPanel').classList.add('show');
}

function confirmWrong() {
  const corrected = document.getElementById('correctedOrder').value;
  decide('wrong', corrected || null);
}

async function decide(decision, corrected_order) {
  if (!currentCrop) return;
  await api('/api/decide', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({crop_path: currentCrop.crop_path, decision, corrected_order: corrected_order || null})
  });
  await loadOrders();
}

async function undo() {
  await api('/api/undo?order=' + encodeURIComponent(currentOrder) + '&tier=' + encodeURIComponent(currentTier), {method: 'POST'});
  await loadOrders();
}

document.addEventListener('keydown', (e) => {
  if (!currentCrop) return;
  if (['SELECT', 'INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
  if (e.key === 'c' || e.key === 'C') decide('correct');
  if (e.key === 'd' || e.key === 'D') decide('discard');
  if (e.key === 'w' || e.key === 'W') toggleWrongPanel();
  if (e.key === 'u' || e.key === 'U') undo();
});

loadOrders();
</script>
</body>
</html>
"""


def make_handler(base_dir, out_dir, db, per_order):

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # quiet

        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _ensure_sampled(self, order):
            if not db.order_sampled(order):
                rows = build_sample(base_dir, order, per_order)
                db.insert_sample(rows)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/":
                html = INDEX_HTML.replace(
                    "ORDER_OPTIONS",
                    "".join(f'<option value="{o}">{o}</option>' for o in ORDERS),
                )
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/orders":
                for order in ORDERS:
                    if os.path.isdir(os.path.join(base_dir, order)):
                        self._ensure_sampled(order)
                progress = db.progress()
                out = []
                for order in ORDERS:
                    p = progress.get(order)
                    if not p:
                        continue
                    reviewed = p["correct"] + p["wrong"] + p["discard"]
                    tiers = {}
                    for tname, t in p["tiers"].items():
                        tiers[tname] = {
                            "total": t["total"], "reviewed": t["correct"] + t["wrong"] + t["discard"],
                            "correct": t["correct"], "wrong": t["wrong"], "discard": t["discard"],
                        }
                    out.append({
                        "order": order, "total": p["total"], "reviewed": reviewed,
                        "correct": p["correct"], "wrong": p["wrong"], "discard": p["discard"],
                        "tiers": tiers,
                    })
                self._send_json({"orders": out})
                return

            if parsed.path == "/api/next":
                order = qs.get("order", [None])[0]
                tier = qs.get("tier", ["all"])[0]
                self._ensure_sampled(order)
                crop = db.next_pending(order, tier)
                self._send_json({"crop": crop})
                return

            if parsed.path == "/image":
                rel = qs.get("path", [None])[0]
                safe_path = os.path.normpath(os.path.join(base_dir, rel))
                if not safe_path.startswith(os.path.normpath(base_dir)):
                    self.send_response(403)
                    self.end_headers()
                    return
                if not os.path.isfile(safe_path):
                    self.send_response(404)
                    self.end_headers()
                    return
                with open(safe_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/api/decide":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                crop_path = body["crop_path"]
                decision = body["decision"]
                corrected_order = body.get("corrected_order")

                row = db.get_row(crop_path)
                if row:
                    order = row["order_name"]
                    dest_folder = {"correct": "verified_correct", "wrong": "verified_wrong", "discard": "discard"}[decision]
                    dest_dir = os.path.join(out_dir, dest_folder, order)
                    os.makedirs(dest_dir, exist_ok=True)
                    src = os.path.join(base_dir, crop_path)
                    fname = os.path.basename(crop_path)
                    if decision == "wrong" and corrected_order:
                        fname = f"correctedas_{corrected_order}__{fname}"
                    dest = os.path.join(dest_dir, fname)
                    if os.path.isfile(src):
                        shutil.copy2(src, dest)
                    db.set_decision(crop_path, decision, corrected_order)
                    db.export_csv(os.path.join(out_dir, "decisions.csv"))
                self._send_json({"ok": True})
                return

            if parsed.path == "/api/undo":
                order = qs.get("order", [None])[0]
                tier = qs.get("tier", ["all"])[0]
                crop_path = db.last_decision(order, tier)
                if crop_path:
                    row = db.get_row(crop_path)
                    dest_folder = {"correct": "verified_correct", "wrong": "verified_wrong", "discard": "discard"}.get(row["decision"])
                    if dest_folder:
                        fname = os.path.basename(crop_path)
                        for candidate in os.listdir(os.path.join(out_dir, dest_folder, order)) if os.path.isdir(os.path.join(out_dir, dest_folder, order)) else []:
                            if candidate == fname or candidate.endswith("__" + fname):
                                try:
                                    os.remove(os.path.join(out_dir, dest_folder, order, candidate))
                                except OSError:
                                    pass
                    db.set_decision(crop_path, "pending", None)
                    db.export_csv(os.path.join(out_dir, "decisions.csv"))
                self._send_json({"ok": True})
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base_dir", required=True, help="Path to classification_by_confidence/ (read-only, never modified)")
    parser.add_argument("--out_dir", required=True, help="Where to write verified_correct/, verified_wrong/, discard/, decisions.csv")
    parser.add_argument("--per_order", type=int, default=1_000_000,
                        help="Cap on crops queued per order. Default is effectively unlimited: the whole "
                             "directory becomes the queue, served one at a time in random order, and the "
                             "reviewer just stops whenever they've had enough -- there's no target to hit.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    db = ReviewDB(os.path.join(args.out_dir, "review_state.db"))
    handler = make_handler(os.path.abspath(args.base_dir), os.path.abspath(args.out_dir), db, args.per_order)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"Review tool running at http://localhost:{args.port}")
        print(f"Base data (read-only): {args.base_dir}")
        print(f"Output goes to: {args.out_dir}")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)


if __name__ == "__main__":
    main()
