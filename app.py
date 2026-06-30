import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify

from signals import llm_signal

app = Flask(__name__)

DB_PATH = "audit_log.db"


# --- Audit log (SQLite) ---
# Milestone 3: persist every classification decision so submissions are auditable
# instead of disappearing once the response is sent.

def init_db():
    # Creates the `decisions` table on startup if it doesn't already exist.
    # Schema matches planning.md's audit log spec. stylo_score/final_score
    # columns exist now but stay NULL until Milestone 4 adds Signal 2 + scoring.
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


def log_decision(content_id, creator_id, content, llm_score, attribution, label_text):
    # Writes one structured row per /submit call: who submitted what, the
    # signal score(s) that drove the decision, and the label text shown to them.
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
                None,  # stylo_score not implemented until Milestone 4
                None,  # final_score not implemented until Milestone 4
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


# --- Routes ---

@app.route("/submit", methods=["POST"])
def submit():
    # Milestone 3: accepts { content, creator_id }, runs Signal 1 (LLM
    # classifier), logs the decision, and returns the result. confidence/label
    # are placeholders here — real combined scoring lands in Milestone 4 and
    # the real label generator in Milestone 5.
    data = request.get_json()

    content = data.get("content")
    creator_id = data.get("creator_id")

    content_id = str(uuid.uuid4())  # generated server-side so the client can't spoof/collide IDs

    llm_result = llm_signal(content)  # Signal 1: Groq LLM classifier -> ai_probability 0.0-1.0
    attribution = "AI" if llm_result["ai_probability"] >= 0.5 else "Human"  # placeholder threshold
    label_text = "We're not sure who wrote this."  # placeholder until Milestone 5's label generator

    log_decision(content_id, creator_id, content, llm_result["ai_probability"], attribution, label_text)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": 0.5,  # placeholder until Milestone 4's combined scoring
        "label": label_text,
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
