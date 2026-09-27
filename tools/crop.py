#!/usr/bin/env python3
"""Crop tool for the website's photos.

Usage (from the repository root):
    python3 tools/crop.py images/activities/2026OrientationWeek
    python3 tools/crop.py images/activities/2026OrientationWeek --port 8002

Then open http://localhost:8001 (or the port you chose).

Every crop is cut from the untouched original in <folder>/uncropped/, so you can
re-crop as often as you like without losing quality. If a picture has no copy in
uncropped/ yet, it is copied there before the first save. The cropped result is
written over <folder>/<name>, which is the file the HTML pages show.

"Match height" reads the site's HTML to find which pictures share a row
(e.g. col-md-9 + col-md-3) and picks the shape that makes this picture exactly
as tall as its neighbour at desktop width.
"""
import argparse
import json
import mimetypes
import re
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
EXTS = {".jpg", ".jpeg", ".png", ".webp"}
FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}

COL_RE = re.compile(r'<div class="[^"]*\bcol-md-(\d+)\b[^"]*">')
# A column that holds nothing but one picture (optionally with <br>s around it).
IMG_COL_RE = re.compile(r'\s*(?:<br>\s*)*<img[^>]*\bsrc="([^"]+)"[^>]*>\s*(?:<br>\s*)*</div>')


def scan_layout():
    """Map each image src to the page, column width and row it sits in."""
    layout = {}
    for page in sorted(ROOT.glob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        row, total = [], 0

        def flush():
            nonlocal row, total
            for src, col in row:
                layout.setdefault(src, {"page": page.name, "col": col, "row": list(row)})
            row, total = [], 0

        for m in COL_RE.finditer(text):
            img = IMG_COL_RE.match(text, m.end())
            if not img:
                flush()
                continue
            col = int(m.group(1))
            if total + col > 12:
                flush()
            row.append((img.group(1), col))
            total += col
            if total >= 12:
                flush()
        flush()
    return layout


def image_size(path):
    """Size as a browser shows it, i.e. after EXIF rotation."""
    with Image.open(path) as im:
        w, h = im.size
        if im.getexif().get(0x0112) in (5, 6, 7, 8):
            w, h = h, w
    return [w, h]


def file_url(path):
    return "/file/" + quote(path.relative_to(ROOT).as_posix())


class Folder:
    def __init__(self, path):
        self.dir = path
        self.orig_dir = path / "uncropped"
        self.crops_file = self.orig_dir / "crops.json"

    def names(self):
        return sorted({p.name for d in (self.dir, self.orig_dir) if d.is_dir()
                       for p in d.iterdir() if p.suffix.lower() in EXTS})

    def original(self, name):
        p = self.orig_dir / name
        return p if p.exists() else self.dir / name

    def crops(self):
        try:
            return json.loads(self.crops_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def listing(self):
        layout = scan_layout()
        crops = self.crops()
        items = []
        for name in self.names():
            out = self.dir / name
            src = out.relative_to(ROOT).as_posix()
            info = layout.get(src, {})
            row = []
            for s, col in info.get("row", []):
                p = ROOT / s
                row.append({"name": Path(s).name, "col": col, "self": s == src,
                            "size": image_size(p) if p.is_file() else None,
                            "url": file_url(p) if p.is_file() else None})
            items.append({
                "name": name,
                "origUrl": file_url(self.original(name)),
                "outUrl": file_url(out) if out.exists() else None,
                "orig": image_size(self.original(name)),
                "out": image_size(out) if out.exists() else None,
                "crop": crops.get(name),
                "page": info.get("page"),
                "col": info.get("col"),
                "row": row,
            })
        return items

    def save_crop(self, name, x, y, w, h, mode):
        out, orig = self.dir / name, self.orig_dir / name
        if not orig.exists():
            self.orig_dir.mkdir(exist_ok=True)
            shutil.copy2(out, orig)
        with Image.open(orig) as src:
            icc = src.info.get("icc_profile")
            im = ImageOps.exif_transpose(src)
        x = max(0, min(round(x), im.width - 1))
        y = max(0, min(round(y), im.height - 1))
        w = max(1, min(round(w), im.width - x))
        h = max(1, min(round(h), im.height - y))
        cropped = im.crop((x, y, x + w, y + h))
        fmt = FORMATS[out.suffix.lower()]
        options = {"icc_profile": icc} if icc else {}
        if fmt == "JPEG":
            cropped = cropped.convert("RGB")
            options.update(quality=90, optimize=True)
        # EXIF (camera info, GPS location) is deliberately not carried over.
        tmp = out.with_name(out.name + ".tmp")
        cropped.save(tmp, fmt, **options)
        tmp.replace(out)
        crops = self.crops()
        crops[name] = {"x": x, "y": y, "w": w, "h": h, "mode": mode}
        self.crops_file.write_text(json.dumps(crops, indent=2) + "\n")
        return [w, h]


class Handler(BaseHTTPRequestHandler):
    folder = None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = PAGE.replace("__FOLDER__", self.folder.dir.relative_to(ROOT).as_posix())
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if path == "/api/images":
            return self.send_json(200, self.folder.listing())
        if path.startswith("/file/"):
            p = (ROOT / unquote(path[len("/file/"):])).resolve()
            if ROOT in p.parents and p.suffix.lower() in EXTS and p.is_file():
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                return self.send(200, p.read_bytes(), ctype)
        self.send(404, b"Not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/crop":
            return self.send(404, b"Not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if body["name"] not in self.folder.names():
                return self.send_json(404, {"error": "Unknown picture"})
            size = self.folder.save_crop(body["name"], float(body["x"]), float(body["y"]),
                                         float(body["w"]), float(body["h"]), str(body.get("mode", "free")))
        except Exception as err:  # report anything back to the page instead of a dropped connection
            return self.send_json(500, {"error": str(err)})
        self.send_json(200, {"size": size})

    def send_json(self, code, data):
        self.send(code, json.dumps(data).encode(), "application/json")

    def send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Crop Tool</title>
<style>
  :root { --bg:#f6f5f2; --panel:#fff; --text:#1d1d1b; --muted:#6b6a64; --line:#e2e0da; --chip:#eeede8;
          --accent:#d9694f; --accent-text:#fff; --warn:#b4541a; --ok:#2f7d4f; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#161615; --panel:#1f1f1d; --text:#ecebe6; --muted:#9a9993; --line:#34332f; --chip:#2a2926;
            --accent:#ef8a70; --accent-text:#1a1a1a; --warn:#f0a36b; --ok:#7cc79a; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
  header { padding: 12px 20px; border-bottom: 1px solid var(--line); display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; }
  header code { color: var(--muted); font-size: 13px; }
  .app { display: grid; grid-template-columns: 250px 1fr; min-height: calc(100vh - 50px); }
  aside { border-right: 1px solid var(--line); padding: 12px; display: flex; flex-direction: column; gap: 6px; overflow: auto; }
  .item { display: flex; gap: 10px; align-items: center; text-align: left; background: none; border: 1px solid transparent;
          border-radius: 8px; padding: 6px; color: inherit; cursor: pointer; font: inherit; }
  .item:hover { background: var(--chip); }
  .item.active { border-color: var(--accent); background: var(--panel); }
  .item img { width: 64px; height: 48px; object-fit: cover; border-radius: 4px; flex: none; background: var(--chip); }
  .item b { display: block; font-weight: 600; font-size: 13px; word-break: break-all; }
  .item small { display: block; color: var(--muted); font-size: 12px; }
  main { padding: 16px 20px 24px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .toolbar .label { color: var(--muted); margin-right: 4px; }
  button.chip, .bar button { font: inherit; color: inherit; background: var(--chip); border: 1px solid var(--line);
          border-radius: 999px; padding: 4px 12px; cursor: pointer; }
  button.chip:hover, .bar button:hover { border-color: var(--muted); }
  button.chip.match { border-style: dashed; border-color: var(--accent); }
  button.chip.on { background: var(--accent); color: var(--accent-text); border-color: var(--accent); border-style: solid; }
  #custom input { width: 64px; font: inherit; padding: 3px 6px; border: 1px solid var(--line); border-radius: 6px;
          background: var(--panel); color: inherit; }
  .hint { margin: 0; color: var(--muted); font-size: 13px; }
  .stage { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px;
          display: flex; justify-content: center; }
  .frame { position: relative; display: inline-block; overflow: hidden; line-height: 0; touch-action: none;
          user-select: none; cursor: crosshair; }
  #photo { display: block; max-width: 100%; max-height: 62vh; -webkit-user-drag: none; }
  #box { position: absolute; box-shadow: 0 0 0 9999px rgba(0,0,0,.55); outline: 1px solid rgba(255,255,255,.95); cursor: move; }
  #box::before, #box::after { content: ""; position: absolute; pointer-events: none; }
  #box::before { left: 33.333%; right: 33.333%; top: 0; bottom: 0;
          border-left: 1px solid rgba(255,255,255,.35); border-right: 1px solid rgba(255,255,255,.35); }
  #box::after { top: 33.333%; bottom: 33.333%; left: 0; right: 0;
          border-top: 1px solid rgba(255,255,255,.35); border-bottom: 1px solid rgba(255,255,255,.35); }
  .h { position: absolute; width: 12px; height: 12px; margin: -6px 0 0 -6px; background: #fff;
          border: 1px solid rgba(0,0,0,.5); border-radius: 2px; z-index: 1; }
  .h[data-h=nw] { left: 0; top: 0; cursor: nwse-resize; }     .h[data-h=n] { left: 50%; top: 0; cursor: ns-resize; }
  .h[data-h=ne] { left: 100%; top: 0; cursor: nesw-resize; }  .h[data-h=e] { left: 100%; top: 50%; cursor: ew-resize; }
  .h[data-h=se] { left: 100%; top: 100%; cursor: nwse-resize; } .h[data-h=s] { left: 50%; top: 100%; cursor: ns-resize; }
  .h[data-h=sw] { left: 0; top: 100%; cursor: nesw-resize; }  .h[data-h=w] { left: 0; top: 50%; cursor: ew-resize; }
  .bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  #readout { font-variant-numeric: tabular-nums; color: var(--muted); }
  .spacer { flex: 1; }
  .bar button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); font-weight: 600; padding: 6px 16px; }
  .bar button:disabled { opacity: .6; cursor: default; }
  #save.dirty::after { content: " \2022"; }
  .rowprev { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
  .rowprev h2 { font-size: 13px; margin: 0 0 2px; }
  #rowinfo { margin: 0 0 10px; color: var(--muted); }
  #rowinfo.warn { color: var(--warn); } #rowinfo.ok { color: var(--ok); }
  #row { display: block; max-width: 100%; }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg);
          padding: 8px 14px; border-radius: 8px; opacity: 0; transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; } .toast.err { background: #b3261e; color: #fff; }
  @media (max-width: 760px) {
    .app { grid-template-columns: 1fr; }
    aside { flex-direction: row; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line); }
    .item { flex: none; }
  }
</style>
</head>
<body>
<header><h1>Photo crop tool</h1><code>__FOLDER__</code></header>
<div class="app">
  <aside id="list"></aside>
  <main>
    <div class="toolbar"><span class="label">Shape</span><span id="modes" class="toolbar"></span>
      <span id="custom" hidden><input id="cw" type="number" min="0.01" step="any" value="4"> :
        <input id="ch" type="number" min="0.01" step="any" value="3"></span></div>
    <p class="hint">Drag inside the box to move it, drag the handles to resize, or drag outside it to draw a new box.
      Click a shape again to reset the box to the largest crop of that shape. Ctrl+S saves.</p>
    <div class="stage"><div class="frame" id="frame"><img id="photo" alt="" draggable="false"><div id="box">
      <i class="h" data-h="nw"></i><i class="h" data-h="n"></i><i class="h" data-h="ne"></i><i class="h" data-h="e"></i>
      <i class="h" data-h="se"></i><i class="h" data-h="s"></i><i class="h" data-h="sw"></i><i class="h" data-h="w"></i>
    </div></div></div>
    <div class="bar"><span id="readout"></span><span class="spacer"></span>
      <button id="reset">Reset to full picture</button><button id="save" class="primary">Save crop</button></div>
    <section class="rowprev"><h2>How it sits on the page (desktop width)</h2><p id="rowinfo"></p><canvas id="row"></canvas></section>
  </main>
</div>
<div class="toast" id="toast"></div>
<script>
const CONTAINER = 1140, GUTTER = 30;            // Bootstrap 4 container at >=1200px, column padding
const PRESETS = ['16:9', '3:2', '4:3', '1:1', '3:4', '2:3', '9:16'];
const colW = c => c / 12 * CONTAINER - GUTTER;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const $ = id => document.getElementById(id);
const photo = $('photo'), box = $('box'), frame = $('frame');
let items = [], cur = null, nat = { w: 1, h: 1 }, rect = null, mode = 'free', ratio = null;
let scale = 1, drag = null, dirty = false, ver = Date.now();
const imgCache = {};

async function load(name) {
  items = await (await fetch('/api/images')).json();
  ver = Date.now();
  renderList(name);
  const pick = items.find(i => i.name === name) || items[0];
  if (pick) select(pick, true);
}

function renderList(activeName) {
  $('list').innerHTML = '';
  for (const it of items) {
    const b = document.createElement('button');
    b.className = 'item' + (it.name === activeName ? ' active' : '');
    b.innerHTML = '<img alt=""><span><b></b><small></small><small></small></span>';
    b.querySelector('img').src = `${it.outUrl || it.origUrl}?v=${ver}`;
    b.querySelector('b').textContent = it.name;
    const [where, state] = b.querySelectorAll('small');
    where.textContent = it.col ? `col-md-${it.col} on ${it.page}` : 'not on any page';
    const edited = it.out && (it.out[0] !== it.orig[0] || it.out[1] !== it.orig[1]);
    state.textContent = it.crop ? `cropped ${it.crop.w} × ${it.crop.h}`
                      : edited ? `cropped elsewhere (${it.out[0]} × ${it.out[1]})` : 'uncropped';
    b.onclick = () => select(it);
    $('list').append(b);
  }
}

function select(it, force) {
  if (!force && dirty && cur && it.name !== cur.name && !confirm('Discard the unsaved crop?')) return;
  cur = it; dirty = false;
  [...$('list').children].forEach((b, i) => b.classList.toggle('active', items[i] === it));
  photo.onload = () => {
    nat = { w: photo.naturalWidth, h: photo.naturalHeight };
    rect = it.crop ? { x: it.crop.x, y: it.crop.y, w: it.crop.w, h: it.crop.h } : { x: 0, y: 0, w: nat.w, h: nat.h };
    renderModes();
    let m = it.crop ? it.crop.mode : 'free';
    if (m.startsWith('custom:')) { const [, a, b] = m.split(':'); $('cw').value = a; $('ch').value = b; m = 'custom'; }
    if (![...$('modes').children].some(b => b.dataset.m === m)) m = 'free';
    setMode(m, true);
  };
  photo.src = `${it.origUrl}?v=${ver}`;
}

const neighbours = () => (cur.row || []).filter(n => !n.self && n.size);

function renderModes() {
  const list = [['free', 'Free'], ['original', 'Original shape']];
  for (const n of neighbours()) list.push([`match:${n.name}`, `Match height of ${n.name}`, 'match']);
  for (const r of PRESETS) list.push([r, r]);
  list.push(['custom', 'Custom']);
  $('modes').innerHTML = '';
  for (const [m, label, cls] of list) {
    const b = document.createElement('button');
    b.className = 'chip' + (cls ? ' ' + cls : '');
    b.dataset.m = m; b.textContent = label;
    if (cls === 'match') b.title = 'Picks the shape that makes this picture exactly as tall as its neighbour in the same row';
    b.onclick = () => setMode(m);
    $('modes').append(b);
  }
}

function ratioFor(m) {
  if (m === 'free') return null;
  if (m === 'original') return nat.w / nat.h;
  if (m === 'custom') { const a = +$('cw').value, b = +$('ch').value; return a > 0 && b > 0 ? a / b : null; }
  if (m.startsWith('match:')) {
    const n = neighbours().find(n => n.name === m.slice(6));
    return n ? colW(cur.col) * n.size[0] / (colW(n.col) * n.size[1]) : null;
  }
  const [a, b] = m.split(':').map(Number);
  return a / b;
}

function fitRatio(r) {
  const w = Math.min(nat.w, nat.h * r), h = w / r;
  const cx = rect.x + rect.w / 2, cy = rect.y + rect.h / 2;
  return { x: clamp(cx - w / 2, 0, nat.w - w), y: clamp(cy - h / 2, 0, nat.h - h), w, h };
}

function setMode(m, restoring) {
  mode = m; ratio = ratioFor(m);
  if (ratio && !(restoring && Math.abs(rect.w / rect.h - ratio) < 0.002)) {
    rect = fitRatio(ratio);
    dirty = true;   // also when restoring: the neighbour changed since this was saved
  }
  if (!restoring) dirty = true;
  [...$('modes').children].forEach(b => b.classList.toggle('on', b.dataset.m === m));
  $('custom').hidden = m !== 'custom';
  draw();
}

const modeKey = () => mode === 'custom' ? `custom:${$('cw').value}:${$('ch').value}` : mode;

function draw() {
  if (!rect) return;
  scale = photo.clientWidth / nat.w;
  Object.assign(box.style, { left: rect.x * scale + 'px', top: rect.y * scale + 'px',
                             width: rect.w * scale + 'px', height: rect.h * scale + 'px' });
  $('readout').textContent = `${Math.round(rect.w)} × ${Math.round(rect.h)} px  ·  shape ${(rect.w / rect.h).toFixed(3)}`;
  $('save').classList.toggle('dirty', dirty);
  drawRow();
}

// Resize from a handle; the opposite side/corner stays put. With a locked shape,
// edge handles grow around the box's centre line.
function resizeTo(hd, s, px, py) {
  const r = ratio, MIN = 16 / scale;
  const E = hd.includes('e'), W = hd.includes('w'), N = hd.includes('n'), S = hd.includes('s');
  const ax = E ? s.x : s.x + s.w, ay = S ? s.y : s.y + s.h;
  const cx = s.x + s.w / 2, cy = s.y + s.h / 2;
  if ((E || W) && (N || S)) {
    const dx = px - ax, dy = py - ay;
    const maxW = dx >= 0 ? nat.w - ax : ax, maxH = dy >= 0 ? nat.h - ay : ay;
    let w = Math.abs(dx), h = Math.abs(dy);
    if (r) { w = Math.min(Math.max(w, h * r), maxW, maxH * r); w = Math.max(w, Math.min(MIN, maxW, maxH * r)); h = w / r; }
    else { w = Math.min(Math.max(w, MIN), maxW); h = Math.min(Math.max(h, MIN), maxH); }
    return { x: dx >= 0 ? ax : ax - w, y: dy >= 0 ? ay : ay - h, w, h };
  }
  if (E || W) {
    const dx = px - ax, maxW = dx >= 0 ? nat.w - ax : ax;
    let w = Math.abs(dx);
    if (!r) { w = Math.min(Math.max(w, MIN), maxW); return { x: dx >= 0 ? ax : ax - w, y: s.y, w, h: s.h }; }
    const maxH = 2 * Math.min(cy, nat.h - cy);
    w = Math.min(w, maxW, maxH * r); w = Math.max(w, Math.min(MIN, maxW, maxH * r));
    return { x: dx >= 0 ? ax : ax - w, y: cy - w / r / 2, w, h: w / r };
  }
  const dy = py - ay, maxH = dy >= 0 ? nat.h - ay : ay;
  let h = Math.abs(dy);
  if (!r) { h = Math.min(Math.max(h, MIN), maxH); return { x: s.x, y: dy >= 0 ? ay : ay - h, w: s.w, h }; }
  const maxW = 2 * Math.min(cx, nat.w - cx);
  h = Math.min(h, maxH, maxW / r); h = Math.max(h, Math.min(MIN, maxH, maxW / r));
  return { x: cx - h * r / 2, y: dy >= 0 ? ay : ay - h, w: h * r, h };
}

function toNat(e, clamped) {
  const b = photo.getBoundingClientRect();
  let x = (e.clientX - b.left) / scale, y = (e.clientY - b.top) / scale;
  if (clamped) { x = clamp(x, 0, nat.w); y = clamp(y, 0, nat.h); }
  return { x, y };
}

frame.addEventListener('pointerdown', e => {
  if (!rect || e.button !== 0) return;
  const hd = e.target.dataset.h;
  if (hd) drag = { kind: hd, start: { ...rect } };
  else if (box.contains(e.target)) drag = { kind: 'move', start: { ...rect }, p0: toNat(e, false) };
  else drag = { kind: 'draw', p0: toNat(e, true) };
  frame.setPointerCapture(e.pointerId);
  e.preventDefault();
});
frame.addEventListener('pointermove', e => {
  if (!drag) return;
  if (drag.kind === 'move') {
    const p = toNat(e, false), s = drag.start;
    rect = { ...s, x: clamp(s.x + p.x - drag.p0.x, 0, nat.w - s.w), y: clamp(s.y + p.y - drag.p0.y, 0, nat.h - s.h) };
  } else {
    const p = toNat(e, true);
    if (drag.kind === 'draw') {
      if (Math.hypot(p.x - drag.p0.x, p.y - drag.p0.y) * scale < 4) return;   // a click, not a drag
      rect = resizeTo('se', { x: drag.p0.x, y: drag.p0.y, w: 0, h: 0 }, p.x, p.y);
    } else rect = resizeTo(drag.kind, drag.start, p.x, p.y);
  }
  dirty = true; draw();
});
['pointerup', 'pointercancel'].forEach(t => frame.addEventListener(t, () => { drag = null; }));

function cached(url) {
  const key = `${url}?v=${ver}`;
  if (!imgCache[key]) { const im = new Image(); im.onload = drawRow; im.src = key; imgCache[key] = im; }
  return imgCache[key];
}

function drawRow() {
  const cv = $('row'), info = $('rowinfo');
  if (!cur.col) {
    cv.hidden = true; info.className = '';
    info.textContent = "This picture isn't used on any page, so there is no row to preview.";
    return;
  }
  cv.hidden = false;
  const cssW = Math.min(cv.parentElement.clientWidth - 28, 900), s = cssW / CONTAINER;
  const cells = []; let off = 0;
  for (const n of cur.row) {
    const w = colW(n.col), a = n.self ? rect.w / rect.h : n.size ? n.size[0] / n.size[1] : null;
    cells.push({ n, x: off + GUTTER / 2, w, h: a ? w / a : 0 });
    off += n.col / 12 * CONTAINER;
  }
  const maxH = Math.max(...cells.map(c => c.h)), cssH = maxH * s + 22, dpr = devicePixelRatio || 1;
  cv.style.width = cssW + 'px'; cv.style.height = cssH + 'px';
  cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr);
  const g = cv.getContext('2d'), css = getComputedStyle(document.body);
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.font = '12px system-ui, sans-serif';
  for (const c of cells) {
    const dx = c.x * s, dw = c.w * s, dh = c.h * s;
    if (c.n.self) g.drawImage(photo, rect.x, rect.y, rect.w, rect.h, dx, 0, dw, dh);
    else if (c.n.url) { const im = cached(c.n.url); if (im.complete && im.naturalWidth) g.drawImage(im, dx, 0, dw, dh); }
    if (c.n.self) { g.strokeStyle = css.getPropertyValue('--accent'); g.lineWidth = 2; g.strokeRect(dx + 1, 1, dw - 2, dh - 2); }
    g.fillStyle = css.getPropertyValue(c.n.self ? '--text' : '--muted');
    g.fillText(`${c.n.self ? 'this · ' : ''}${Math.round(c.w)} × ${Math.round(c.h)} px`, dx, maxH * s + 15);
  }
  const hs = cells.filter(c => c.h).map(c => c.h), diff = Math.round(Math.max(...hs) - Math.min(...hs));
  if (cells.length < 2) { info.className = ''; info.textContent = `Full-width picture on ${cur.page}.`; }
  else if (diff <= 1) { info.className = 'ok'; info.textContent = `Heights match in this row on ${cur.page}.`; }
  else { info.className = 'warn'; info.textContent = `Heights in this row differ by ${diff} px on ${cur.page}.`; }
}

function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.className = 'toast', 2600);
}

async function save() {
  if (!cur || !rect || $('save').disabled) return;
  const edited = cur.out && !cur.crop && (cur.out[0] !== cur.orig[0] || cur.out[1] !== cur.orig[1]);
  if (edited && !confirm(`${cur.name} was cropped outside this tool. Saving replaces that crop with this one. Continue?`)) return;
  $('save').disabled = true;
  try {
    const res = await fetch('/api/crop', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: cur.name, ...rect, mode: modeKey() }) });
    const out = await res.json();
    if (!res.ok) throw new Error(out.error || res.statusText);
    dirty = false;
    toast(`Saved ${cur.name} (${out.size[0]} × ${out.size[1]} px)`);
    await load(cur.name);
  } catch (err) { toast('Save failed: ' + err.message, true); }
  finally { $('save').disabled = false; }
}

$('save').onclick = save;
$('reset').onclick = () => {
  rect = { x: 0, y: 0, w: nat.w, h: nat.h };
  if (ratio) rect = fitRatio(ratio);
  dirty = true; draw();
};
['cw', 'ch'].forEach(id => $(id).addEventListener('input', () => { if (mode === 'custom') setMode('custom'); }));
addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); } });
addEventListener('resize', draw);
addEventListener('beforeunload', e => { if (dirty) e.preventDefault(); });
load();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Crop the website's photos in the browser.")
    ap.add_argument("folder", help="picture folder, e.g. images/activities/2026OrientationWeek")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()
    folder = Path(args.folder).resolve()
    if not folder.is_dir() or ROOT not in folder.parents:
        sys.exit(f"{args.folder} is not a folder inside {ROOT}")
    Handler.folder = Folder(folder)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Cropping {folder.relative_to(ROOT)} at http://localhost:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
