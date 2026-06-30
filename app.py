import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify

from signals import llm_signal, stylo_signal

app = Flask(__name__)

DB_PATH = "audit_log.db"


# --- Audit log (SQLite) ---
# Milestone 3: persist every classification decision so submissions are auditable
# instead of disappearing once the response is sent.

def init_db():
    # Creates the `decisions` table on startup if it doesn't already exist.
    # Schema matches planning.md's audit log spec.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                content_id      TEXT PRIMARY KEY,
                creator_id      TEXT,
                submitted_at    TEXT,
                content_preview TEXT,
                llm_score       REAL,
                stylo_score     REAL,
                final_score     REAL,
                attribution     TEXT,
                label_text      TEXT,
                status          TEXT
            )
        """)


def log_decision(content_id, creator_id, content, llm_score, stylo_score, final_score, attribution, label_text):
    # Writes one structured row per /submit call: who submitted what, both
    # signal scores, the combined score, and the label text shown to them.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO decisions
                (content_id, creator_id, submitted_at, content_preview,
                 llm_score, stylo_score, final_score, attribution, label_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                creator_id,
                datetime.now(timezone.utc).isoformat(),
                content[:200],  # only store a preview, not the full submitted text
                llm_score,
                stylo_score,
                final_score,
                attribution,
                label_text,
                "decided",
            ),
        )


def get_log(limit=20):
    # Reads back the most recent audit log entries for GET /log.
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row  # lets us convert each row to a dict below
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


init_db()  # ensure the table exists before the app starts handling requests


# --- Confidence scoring (Milestone 4) ---

def compute_confidence(content, llm_score, stylo_score):
    # Weights per planning.md: short texts (<80 words) get near-zero weight on
    # the stylometric signal, since that signal needs sample size to be reliable.
    word_count = len(content.split())
    if word_count < 80:
        return (0.90 * llm_score) + (0.10 * stylo_score)
    return (0.65 * llm_score) + (0.35 * stylo_score)


def attribution_for(score):
    # Threshold bands per planning.md: wide uncertain band (0.35-0.64) so the
    # system admits uncertainty instead of forcing a binary call on borderline text.
    if score >= 0.65:
        return "AI"
    if score <= 0.34:
        return "Human"
    return "Uncertain"


# --- Routes ---

@app.route("/submit", methods=["POST"])
def submit():
    # Milestone 4: runs both detection signals, combines them into a single
    # confidence score per planning.md's weighted formula, and logs the full
    # signal breakdown. label_text is still a placeholder until Milestone 5's
    # label generator.
    data = request.get_json()

    content = data.get("content")
    creator_id = data.get("creator_id")

    content_id = str(uuid.uuid4())  # generated server-side so the client can't spoof/collide IDs

    llm_score = llm_signal(content)["ai_probability"]  # Signal 1: Groq LLM classifier
    stylo_score = stylo_signal(content)["stylo_score"]  # Signal 2: stylometric heuristics

    confidence = compute_confidence(content, llm_score, stylo_score)
    attribution = attribution_for(confidence)
    label_text = "We're not sure who wrote this."  # placeholder until Milestone 5's label generator

    log_decision(content_id, creator_id, content, llm_score, stylo_score, confidence, attribution, label_text)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": label_text,
        "signals": {
            "llm_score": llm_score,
            "stylo_score": stylo_score,
        },
})

@app.route("/log", methods=["GET"])
def view_log():
    # Milestone 3: exposes the audit log as JSON so decisions can be inspected/graded.
    return jsonify({"entries": get_log()})

@app.route("/")
def home():
    return "Provenance Guard is running."

if __name__ == "__main__":
    app.run(port=5000, debug=True)
