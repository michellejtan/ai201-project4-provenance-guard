import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify

from signals import llm_signal

app = Flask(__name__)

DB_PATH = "audit_log.db"


def init_db():
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
                content[:200],
                llm_score,
                None,  # stylo_score not implemented until Milestone 4
                None,  # final_score not implemented until Milestone 4
                attribution,
                label_text,
                "decided",
            ),
        )


init_db()


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json()

    content = data.get("content")
    creator_id = data.get("creator_id")

    content_id = str(uuid.uuid4()) # create once

    llm_result = llm_signal(content)
    attribution = "AI" if llm_result["ai_probability"] >= 0.5 else "Human"
    label_text = "We're not sure who wrote this."  # placeholder until Milestone 5's label generator

    log_decision(content_id, creator_id, content, llm_result["ai_probability"], attribution, label_text)

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": 0.5,  # placeholder until Milestone 4's combined scoring
        "label": label_text,
})

@app.route("/")
def home():
    return "Provenance Guard is running."

if __name__ == "__main__":
    app.run(port=5000, debug=True)