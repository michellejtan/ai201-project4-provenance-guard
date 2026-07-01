import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from signals import llm_signal, stylo_signal, caption_signal

app = Flask(__name__)

# Per-IP rate limits (see README.md's Rate Limiting section for the reasoning
# behind each number).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

DB_PATH = "audit_log.db"

# Pool of single-use identity-verification codes, simulating an out-of-band
# check (e.g. distributed at course enrollment or via email confirmation).
# Redeeming one via POST /verify earns the creator a provenance certificate.
_DEMO_VERIFICATION_CODES = [f"VERIFY-DEMO-{i}" for i in range(1, 11)]

_CERTIFICATE_TEXT = (
    "✓ Verified Human Creator\n\n"
    "This creator has completed an independent identity-verification step. "
    "This badge certifies the creator's identity — it is not a per-submission "
    "content analysis, and does not affect the Attribution label above."
)


# --- Audit log (SQLite) ---
# Milestone 3: persist every classification decision so submissions are auditable
# instead of disappearing once the response is sent.

def init_db():
    # Creates the `decisions` table on startup if it doesn't already exist.
    # Schema matches planning.md's audit log spec.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                content_id       TEXT PRIMARY KEY,
                creator_id       TEXT,
                submitted_at     TEXT,
                content_type     TEXT,
                content_preview  TEXT,
                llm_score        REAL,
                stylo_score      REAL,
                final_score      REAL,
                attribution      TEXT,
                label_text       TEXT,
                creator_verified INTEGER,
                status           TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appeals (
                appeal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id  TEXT,
                creator_id  TEXT,
                appealed_at TEXT,
                reasoning   TEXT
            )
        """)
        # --- Provenance certificate (Milestone 6 extra credit) ---
        # `creators`: which creator_ids have earned the "verified human" badge.
        # `verification_codes`: a pool of single-use codes simulating an
        # out-of-band identity check (e.g. handed out at course enrollment).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS creators (
                creator_id  TEXT PRIMARY KEY,
                verified_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verification_codes (
                code    TEXT PRIMARY KEY,
                used_by TEXT,
                used_at TEXT
            )
        """)
        for code in _DEMO_VERIFICATION_CODES:
            conn.execute(
                "INSERT OR IGNORE INTO verification_codes (code, used_by, used_at) VALUES (?, NULL, NULL)",
                (code,),
            )


def log_decision(content_id, creator_id, content, content_type, llm_score, stylo_score,
                  final_score, attribution, label_text, creator_verified):
    # Writes one structured row per /submit call: who submitted what, both
    # signal scores, the combined score, and the label text shown to them.
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO decisions
                (content_id, creator_id, submitted_at, content_type, content_preview,
                 llm_score, stylo_score, final_score, attribution, label_text,
                 creator_verified, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id,
                creator_id,
                datetime.now(timezone.utc).isoformat(),
                content_type,
                content[:200],  # only store a preview, not the full submitted text
                llm_score,
                stylo_score,
                final_score,
                attribution,
                label_text,
                int(creator_verified),
                "decided",
            ),
        )


def is_creator_verified(creator_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM creators WHERE creator_id = ?", (creator_id,)
        ).fetchone()
    return row is not None


def verify_creator(creator_id, code):
    # Redeems a single-use verification code for a creator_id. Returns True
    # if the code was valid and unused, False otherwise (already used, or
    # doesn't exist).
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT used_by FROM verification_codes WHERE code = ?", (code,)
        ).fetchone()
        if row is None or row[0] is not None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE verification_codes SET used_by = ?, used_at = ? WHERE code = ?",
            (creator_id, now, code),
        )
        conn.execute(
            "INSERT OR REPLACE INTO creators (creator_id, verified_at) VALUES (?, ?)",
            (creator_id, now),
        )
        return True


def get_decision(content_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM decisions WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def log_appeal(content_id, creator_id, reasoning):
    # Flips the decision to under_review and appends a linked appeal entry,
    # matching planning.md's Appeal Flow (no automatic re-classification).
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE decisions SET status = 'under_review' WHERE content_id = ?",
            (content_id,),
        )
        conn.execute(
            "INSERT INTO appeals (content_id, creator_id, appealed_at, reasoning) VALUES (?, ?, ?, ?)",
            (content_id, creator_id, datetime.now(timezone.utc).isoformat(), reasoning),
        )


def get_log(limit=20):
    # Reads back the most recent audit log entries for GET /log.
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row  # lets us convert each row to a dict below
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY submitted_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_analytics():
    # Milestone 6 extra credit: aggregate metrics across the audit log.
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        decisions = [dict(r) for r in conn.execute("SELECT * FROM decisions").fetchall()]
        appeal_count = conn.execute("SELECT COUNT(*) FROM appeals").fetchone()[0]
        verified_creator_count = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]

    total = len(decisions)

    # Metric 1: detection pattern — ratio of AI vs Human vs Uncertain verdicts.
    breakdown = {"AI": 0, "Human": 0, "Uncertain": 0}
    for d in decisions:
        breakdown[d["attribution"]] = breakdown.get(d["attribution"], 0) + 1
    attribution_ratio = {
        k: round(v / total, 4) if total else 0.0 for k, v in breakdown.items()
    }

    # Metric 2: appeal rate — appeals filed per submission.
    appeal_rate = round(appeal_count / total, 4) if total else 0.0

    # Metric 3 (chosen metric): signal agreement rate — how often the two
    # underlying signals (llm vs. stylo/caption) land on the same side of the
    # 0.5 midpoint. Low agreement flags content where the signals are
    # fighting each other, which is exactly the content most worth a human's
    # attention regardless of which side the combined score fell on.
    agree = 0
    for d in decisions:
        if (d["llm_score"] >= 0.5) == (d["stylo_score"] >= 0.5):
            agree += 1
    signal_agreement_rate = round(agree / total, 4) if total else 0.0

    return {
        "total_submissions": total,
        "detection_pattern": {
            "counts": breakdown,
            "ratio": attribution_ratio,
        },
        "appeal_rate": appeal_rate,
        "signal_agreement_rate": signal_agreement_rate,
        "verified_creator_count": verified_creator_count,
    }


init_db()  # ensure the table exists before the app starts handling requests


# --- Confidence scoring (Milestone 4) ---

def compute_confidence(content, llm_score, second_score, content_type):
    # Weights per planning.md: short texts (<80 words) get near-zero weight on
    # the stylometric signal, since that signal needs sample size to be reliable.
    # Image captions use a flat 0.5/0.5 split instead: the word-count-based
    # split doesn't apply to inherently short caption text, and the caption
    # signal hasn't been validated enough to justify weighting it above parity.
    if content_type == "image_caption":
        return (0.50 * llm_score) + (0.50 * second_score)
    word_count = len(content.split())
    if word_count < 80:
        return (0.90 * llm_score) + (0.10 * second_score)
    return (0.65 * llm_score) + (0.35 * second_score)


def attribution_for(score):
    # Threshold bands per planning.md: wide uncertain band (0.35-0.64) so the
    # system admits uncertainty instead of forcing a binary call on borderline text.
    if score >= 0.65:
        return "AI"
    if score <= 0.34:
        return "Human"
    return "Uncertain"


# --- Transparency labels (Milestone 5) ---
# Full text for each variant, from planning.md's Transparency Labels section.

_LABEL_TEXT = {
    "AI": (
        "Attribution: Likely AI-Generated\n"
        "Confidence: High\n\n"
        "Our system analyzed the writing patterns and style of this piece and found strong "
        "indicators that it was generated with AI assistance. This is not a definitive judgment "
        "— our detection tools are not perfect, and some human writing styles can resemble "
        "AI-generated text.\n\n"
        "If you created this work yourself, you can submit an appeal and explain your process. "
        "We will review it and update this label."
    ),
    "Human": (
        "Attribution: Likely Human-Written\n"
        "Confidence: High\n\n"
        "Our system analyzed the writing patterns and style of this piece and found strong "
        "indicators that it was written by a person. The language, structure, and style all "
        "appear consistent with human authorship.\n\n"
        "If this assessment seems wrong, you can submit an appeal at any time."
    ),
    "Uncertain": (
        "Attribution: Unclear\n"
        "Confidence: Low\n\n"
        "Our system analyzed this piece but could not make a confident determination about "
        "whether it was written by a person or generated with AI assistance. This can happen "
        "with short texts, experimental writing styles, or pieces that blend human and AI work.\n\n"
        "No action is taken based on an uncertain result. If you want to provide more context "
        "about how you created this work, you can submit an appeal."
    ),
}


def label_for(attribution):
    return _LABEL_TEXT[attribution]


# --- Routes ---

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per hour")
def submit():
    # Runs both detection signals, combines them into a single confidence
    # score (Milestone 4), and generates the matching transparency label
    # (Milestone 5). `content_type` selects which second signal runs
    # alongside the shared LLM signal (Milestone 6: multi-modal support).
    data = request.get_json()

    content = data.get("content")
    creator_id = data.get("creator_id")
    content_type = data.get("content_type", "text")

    if content_type not in ("text", "image_caption"):
        return jsonify({"error": "content_type must be 'text' or 'image_caption'"}), 422

    content_id = str(uuid.uuid4())  # generated server-side so the client can't spoof/collide IDs

    llm_score = llm_signal(content)["ai_probability"]  # Signal 1: Groq LLM classifier, shared across content types
    if content_type == "image_caption":
        second_score = caption_signal(content)["caption_score"]  # Signal 2: caption-specific heuristics
    else:
        second_score = stylo_signal(content)["stylo_score"]  # Signal 2: stylometric heuristics

    confidence = compute_confidence(content, llm_score, second_score, content_type)
    attribution = attribution_for(confidence)
    label_text = label_for(attribution)

    creator_verified = is_creator_verified(creator_id)

    log_decision(content_id, creator_id, content, content_type, llm_score, second_score,
                 confidence, attribution, label_text, creator_verified)

    response = {
        "content_id": content_id,
        "content_type": content_type,
        "attribution": attribution,
        "confidence": confidence,
        "label": label_text,
        "signals": {
            "llm_score": llm_score,
            "caption_score" if content_type == "image_caption" else "stylo_score": second_score,
        },
    }
    # Provenance certificate (Milestone 6): shown as a separate field from
    # the attribution label so it's visually distinguishable — it certifies
    # the creator's identity, not this piece of content.
    if creator_verified:
        response["certificate"] = _CERTIFICATE_TEXT

    return jsonify(response)

@app.route("/appeal", methods=["POST"])
@limiter.limit("3 per hour")
def appeal():
    # Milestone 5: creator disputes a decision. Looks up the original
    # submission, validates the creator matches, then flips status to
    # under_review and logs the appeal. No automatic re-classification —
    # a human reviewer reads the audit log later.
    data = request.get_json()

    content_id = data.get("content_id")
    creator_id = data.get("creator_id")
    reasoning = data.get("reasoning", "")

    if not reasoning or len(reasoning) < 10:
        return jsonify({"error": "reasoning must be at least 10 characters"}), 422

    decision = get_decision(content_id)
    if decision is None:
        return jsonify({"error": "content_id not found"}), 404

    if decision["creator_id"] != creator_id:
        return jsonify({"error": "creator_id does not match the original submission"}), 403

    log_appeal(content_id, creator_id, reasoning)

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal has been received and this content is now under review.",
    })

@app.route("/log", methods=["GET"])
@limiter.limit("30 per hour")
def view_log():
    # Milestone 3: exposes the audit log as JSON so decisions can be inspected/graded.
    return jsonify({"entries": get_log()})

@app.route("/verify", methods=["POST"])
@limiter.limit("5 per hour")
def verify():
    # Milestone 6 extra credit: redeems a single-use verification code to
    # earn the creator a provenance certificate. This is the "additional
    # verification step" — a stand-in for an out-of-band identity check
    # (e.g. a code emailed after enrollment/account confirmation).
    data = request.get_json()

    creator_id = data.get("creator_id")
    verification_code = data.get("verification_code")

    if not creator_id or not verification_code:
        return jsonify({"error": "creator_id and verification_code are required"}), 422

    if is_creator_verified(creator_id):
        return jsonify({
            "creator_id": creator_id,
            "verified": True,
            "certificate": _CERTIFICATE_TEXT,
        })

    if not verify_creator(creator_id, verification_code):
        return jsonify({"error": "verification_code is invalid or already used"}), 403

    return jsonify({
        "creator_id": creator_id,
        "verified": True,
        "certificate": _CERTIFICATE_TEXT,
    })

@app.route("/analytics", methods=["GET"])
@limiter.limit("30 per hour")
def analytics():
    # Milestone 6 extra credit: dashboard metrics computed live from the audit log.
    return jsonify(get_analytics())

@app.route("/")
def home():
    return "Provenance Guard is running."

if __name__ == "__main__":
    app.run(port=5000, debug=True)
