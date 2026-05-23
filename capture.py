"""
LifeReplay — Windows Screen Capture Backend
Captures screen every N seconds, runs OCR, stores in SQLite with FTS5 + embeddings
"""

import time
import sqlite3
import hashlib
import base64
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from io import BytesIO

# ── Third-party (installed via requirements.txt) ──────────────────────────────
try:
    import mss
    import mss.tools
    from PIL import Image
    import pytesseract
    import numpy as np
    from flask import Flask, jsonify, request
    from flask_cors import CORS
    import psutil
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Run: pip install -r requirements.txt")
    sys.exit(1)

# Optional: sentence-transformers for semantic search
try:
    from sentence_transformers import SentenceTransformer
    SEMANTIC_ENABLED = True
    print("[INFO] Semantic search enabled (MiniLM loaded)")
except ImportError:
    SEMANTIC_ENABLED = False
    print("[INFO] Semantic search disabled — install sentence-transformers for it")

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / "LifeReplay"
DATA_DIR.mkdir(exist_ok=True)
THUMBS_DIR = DATA_DIR / "thumbnails"
THUMBS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "memories.db"

CAPTURE_INTERVAL = 5        # seconds between captures
THUMB_SIZE = (180, 320)     # thumbnail dimensions
MIN_TEXT_LENGTH = 20        # skip captures with very little text
DEDUP_THRESHOLD = 0.95      # skip if >95% same as last capture

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Main memories table
    c.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id          TEXT PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            app_name    TEXT,
            window_title TEXT,
            ocr_text    TEXT,
            thumb_path  TEXT,
            screenshot_hash TEXT,
            audio_text  TEXT,
            embedding   BLOB,
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    # FTS5 virtual table for fast keyword search
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(
            id UNINDEXED,
            ocr_text,
            app_name,
            window_title,
            audio_text,
            content='memories',
            content_rowid='rowid'
        )
    """)

    # Triggers to keep FTS in sync
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, id, ocr_text, app_name, window_title, audio_text)
            VALUES (new.rowid, new.id, new.ocr_text, new.app_name, new.window_title, new.audio_text);
        END
    """)
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, id, ocr_text, app_name, window_title, audio_text)
            VALUES ('delete', old.rowid, old.id, old.ocr_text, old.app_name, old.window_title, old.audio_text);
        END
    """)

    conn.commit()
    conn.close()
    print(f"[DB] Database ready at {DB_PATH}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_active_window_info():
    """Get current foreground window title and app name on Windows."""
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buf, length)
        title = buf.value or "Unknown"

        # Get process name
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value)
            app = proc.name().replace(".exe", "")
        except Exception:
            app = "Unknown"
        return app, title
    except Exception:
        return "Unknown", "Unknown"


def image_hash(img: Image.Image) -> str:
    """Quick perceptual hash for deduplication."""
    small = img.resize((16, 16)).convert("L")
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return hex(int(bits, 2))[2:]


def hamming_distance(h1: str, h2: str) -> float:
    """Returns similarity 0-1 between two hashes."""
    try:
        b1 = bin(int(h1, 16))[2:].zfill(256)
        b2 = bin(int(h2, 16))[2:].zfill(256)
        same = sum(a == b for a, b in zip(b1, b2))
        return same / 256
    except Exception:
        return 0.0


def save_thumbnail(img: Image.Image, mem_id: str) -> str:
    img_copy = img.copy()
    img_copy.thumbnail(THUMB_SIZE, Image.LANCZOS)
    path = THUMBS_DIR / f"{mem_id}.jpg"
    img_copy.save(path, "JPEG", quality=60)
    return str(path)


def img_to_base64(img: Image.Image) -> str:
    buf = BytesIO()
    img.thumbnail(THUMB_SIZE)
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


# ── Capture Loop ──────────────────────────────────────────────────────────────
_last_hash = None
_embedding_model = None
_capture_running = False
_capture_thread = None
_stats = {"captured": 0, "skipped_dup": 0, "skipped_empty": 0}


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None and SEMANTIC_ENABLED:
        print("[INFO] Loading MiniLM model (first time may take a moment)...")
        _embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embedding_model


def capture_once():
    global _last_hash

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        screenshot = sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

    # Deduplication
    current_hash = image_hash(img)
    if _last_hash and hamming_distance(current_hash, _last_hash) > DEDUP_THRESHOLD:
        _stats["skipped_dup"] += 1
        return None

    # OCR
    ocr_text = pytesseract.image_to_string(img, config="--psm 6").strip()
    if len(ocr_text) < MIN_TEXT_LENGTH:
        _stats["skipped_empty"] += 1
        _last_hash = current_hash
        return None

    _last_hash = current_hash

    # Window info
    app_name, window_title = get_active_window_info()

    # Generate ID
    now = datetime.now()
    mem_id = f"MEM-{now.strftime('%Y%m%d%H%M%S')}-{current_hash[:6].upper()}"

    # Save thumbnail
    thumb_path = save_thumbnail(img, mem_id)

    # Embedding
    embedding_blob = None
    model = get_embedding_model()
    if model:
        emb = model.encode(ocr_text[:512])
        embedding_blob = emb.tobytes()

    # Store in DB
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO memories
              (id, timestamp, app_name, window_title, ocr_text, thumb_path, screenshot_hash, embedding)
            VALUES (?,?,?,?,?,?,?,?)
        """, (mem_id, now.isoformat(), app_name, window_title, ocr_text,
              thumb_path, current_hash, embedding_blob))
        conn.commit()
        _stats["captured"] += 1
        print(f"[CAPTURE] {mem_id} | {app_name} | {len(ocr_text)} chars")
        return mem_id
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

    return None


def capture_loop():
    global _capture_running
    print(f"[CAPTURE] Starting loop — every {CAPTURE_INTERVAL}s")
    while _capture_running:
        try:
            capture_once()
        except Exception as e:
            print(f"[CAPTURE ERROR] {e}")
        time.sleep(CAPTURE_INTERVAL)


# ── Flask REST API ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


def row_to_dict(row, cursor):
    return {k[0]: v for k, v in zip(cursor.description, row)}


@app.route("/api/memories")
def get_memories():
    limit  = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    app_filter = request.args.get("app", "")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if app_filter:
        c.execute("""
            SELECT id, timestamp, app_name, window_title,
                   substr(ocr_text, 1, 300) as ocr_text, thumb_path
            FROM memories WHERE app_name LIKE ?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (f"%{app_filter}%", limit, offset))
    else:
        c.execute("""
            SELECT id, timestamp, app_name, window_title,
                   substr(ocr_text, 1, 300) as ocr_text, thumb_path
            FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset))

    rows = [row_to_dict(r, c) for r in c.fetchall()]
    conn.close()

    # Attach base64 thumbnails
    for r in rows:
        thumb = r.get("thumb_path")
        if thumb and os.path.exists(thumb):
            with open(thumb, "rb") as f:
                r["thumb_b64"] = base64.b64encode(f.read()).decode()
        else:
            r["thumb_b64"] = None

    return jsonify({"memories": rows, "total": len(rows)})


@app.route("/api/search/keyword")
def search_keyword():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": [], "mode": "keyword"})

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT m.id, m.timestamp, m.app_name, m.window_title,
               snippet(memories_fts, 1, '<mark>', '</mark>', '…', 20) as snippet,
               m.thumb_path
        FROM memories_fts f
        JOIN memories m ON m.id = f.id
        WHERE memories_fts MATCH ?
        ORDER BY rank LIMIT 30
    """, (q,))
    rows = [row_to_dict(r, c) for r in c.fetchall()]
    conn.close()
    return jsonify({"results": rows, "mode": "keyword", "query": q})


@app.route("/api/search/semantic")
def search_semantic():
    q = request.args.get("q", "").strip()
    if not q or not SEMANTIC_ENABLED:
        return jsonify({"results": [], "mode": "semantic",
                        "error": "Semantic search not available" if not SEMANTIC_ENABLED else ""})

    model = get_embedding_model()
    query_emb = model.encode(q)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, timestamp, app_name, window_title, substr(ocr_text,1,200), thumb_path, embedding FROM memories WHERE embedding IS NOT NULL LIMIT 500")
    rows = c.fetchall()
    conn.close()

    results = []
    for row in rows:
        emb_blob = row[6]
        if not emb_blob:
            continue
        emb = np.frombuffer(emb_blob, dtype=np.float32)
        # Cosine similarity
        sim = float(np.dot(query_emb, emb) / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-9))
        results.append({
            "id": row[0], "timestamp": row[1], "app_name": row[2],
            "window_title": row[3], "ocr_text": row[4], "thumb_path": row[5],
            "similarity": round(sim * 100, 1)
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return jsonify({"results": results[:20], "mode": "semantic", "query": q})


@app.route("/api/memory/<mem_id>")
def get_memory(mem_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM memories WHERE id=?", (mem_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    d = row_to_dict(row, c)
    d.pop("embedding", None)  # don't send blob
    if d.get("thumb_path") and os.path.exists(d["thumb_path"]):
        with open(d["thumb_path"], "rb") as f:
            d["thumb_b64"] = base64.b64encode(f.read()).decode()
    return jsonify(d)


@app.route("/api/stats")
def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM memories")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
    with_emb = c.fetchone()[0]
    c.execute("SELECT app_name, COUNT(*) as cnt FROM memories GROUP BY app_name ORDER BY cnt DESC LIMIT 10")
    apps = [{"app": r[0], "count": r[1]} for r in c.fetchall()]
    c.execute("SELECT COUNT(*) FROM memories WHERE date(timestamp) = date('now')")
    today = c.fetchone()[0]
    conn.close()
    return jsonify({
        "total_memories": total,
        "today": today,
        "with_embeddings": with_emb,
        "semantic_enabled": SEMANTIC_ENABLED,
        "capture_running": _capture_running,
        "runtime_stats": _stats,
        "top_apps": apps,
        "db_size_mb": round(os.path.getsize(DB_PATH) / 1024 / 1024, 2) if DB_PATH.exists() else 0
    })


@app.route("/api/capture/start", methods=["POST"])
def start_capture():
    global _capture_running, _capture_thread
    if _capture_running:
        return jsonify({"status": "already running"})
    _capture_running = True
    _capture_thread = threading.Thread(target=capture_loop, daemon=True)
    _capture_thread.start()
    return jsonify({"status": "started"})


@app.route("/api/capture/stop", methods=["POST"])
def stop_capture():
    global _capture_running
    _capture_running = False
    return jsonify({"status": "stopped"})


@app.route("/api/apps")
def get_apps():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT app_name FROM memories ORDER BY app_name")
    apps = [r[0] for r in c.fetchall() if r[0]]
    conn.close()
    return jsonify({"apps": apps})


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  LifeReplay Backend — Windows")
    print("=" * 50)
    init_db()

    # Auto-start capture
    _capture_running = True
    _capture_thread = threading.Thread(target=capture_loop, daemon=True)
    _capture_thread.start()

    print(f"[API] Starting server at http://localhost:5000")
    print(f"[DATA] Saving to {DATA_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
