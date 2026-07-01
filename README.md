# Provenance Guard — README.md

Provenance Guard is a backend API that classifies submitted text as likely AI-generated or
human-written, returns a confidence score, generates a transparency label (high-confidence AI, high-confidence human, or uncertain) for users, and
handles creator appeals. It is designed to plug into any creative-sharing platform.

---

## System Architecture

A submitted piece of text travels through two independent detection signals — an LLM
classifier (Groq) and a stylometric heuristic analyzer — whose outputs are combined into
a single confidence score. That score maps to one of three transparency label variants,
the full decision is written to a SQLite audit log, and the result is returned to the caller.

An appeal enters separately: the system looks up the original decision, validates the
creator, flips the status to `under_review`, appends the appeal to the audit log, and
returns a confirmation. No automatic re-classification happens.

See [planning.md](planning.md) for the full architecture diagram, signal design rationale,
and the AI Tool Plan used to generate implementation code.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/submit` | Submit content for classification |
| `POST` | `/appeal` | Submit an appeal for a prior decision |
| `GET` | `/log` | Retrieve recent audit log entries |

### POST /submit

**Request body:**
```json
{
  "creator_id": "user-42",
  "content": "The text to classify..."
}
```

`content_id` is generated server-side (not supplied by the client) to avoid collisions and
client-spoofed IDs. It is returned in the response and used for appeals.

**Response:**
```json
{
  "content_id": "3f7a2b1e-...",
  "attribution": "AI",
  "confidence": 0.81,
  "label_text": "Attribution: Likely AI-Generated\n...",
  "signals": {
    "llm_score": 0.88,
    "stylo_score": 0.67
  }
}
```

**HTTP status codes:**
- `200` — Classification completed successfully
- `422` — Missing or invalid request data (e.g., missing `content`)
- `429` — Rate limit exceeded

### POST /appeal

**Request body:**
```json
{
  "content_id": "poem-001",
  "creator_id": "user-42",
  "reasoning": "I wrote this poem by hand over two weeks. It reflects my personal style."
}
```

**Response:**
```json
{
  "content_id": "poem-001",
  "status": "under_review",
  "message": "Your appeal has been received and this content is now under review."
}
```

**HTTP status codes:**
- `200` — Appeal accepted
- `403` — `creator_id` does not match the original submission
- `404` — `content_id` not found
- `422` — `reasoning` is fewer than 10 characters
- `429` — Rate limit exceeded

### GET /log

Returns the most recent audit log entries ordered by submission time (newest first).

**HTTP status codes:**
- `200` — Log entries returned successfully
- `429` — Rate limit exceeded

---

## Detection Signals

### Signal 1 — LLM Classifier (Groq llama-3.3-70b-versatile)

Sends the text to the LLM with a structured prompt asking it to assess whether the writing
reads as AI-generated or human-written. Returns a probability score and brief reasoning.

- **Measures:** Semantic coherence, stylistic consistency, formulaic phrasing, naturalness of
  voice, structural predictability.
- **Output:** `llm_score` float 0.0–1.0 (higher = more AI-like). The LLM also returns a `reasoning` string that is intentionally discarded — storing model-generated explanations that may be inconsistent across model versions adds noise without value. Only the numerical score is retained.
- **Blind spot:** Formal human writing (academic papers, legal memos) often scores high
  because it shares tonal uniformity with AI output.

### Signal 2 — Stylometric Heuristics (pure Python)

Computes five measurable statistical properties of the text and combines them into a single
score. Each metric captures a different structural dimension.

| Metric | What it captures | AI pattern |
|---|---|---|
| Sentence length variance | Rhythmic consistency | AI = very uniform (low variance) |
| Type-token ratio (TTR) | Vocabulary diversity | AI = moderate; humans more irregular |
| Punctuation density | Expressive punctuation | AI = sparse and regular |
| Average word length | Diction register | AI = slightly elevated |
| Paragraph/sentence ratio | Structural regularity | AI = even distribution |

- **Output:** `stylo_score` float 0.0–1.0 (higher = more AI-like)
- **Blind spot:** Short or poetic texts (< 80 words) lack enough statistical signal. Metric
  reliability drops and the signal is given near-zero weight in those cases.

### Combination Formula

Normal text (≥ 80 words):
```
final_score = 0.65 × llm_score + 0.35 × stylo_score
```

Short text (< 80 words):
```
final_score = 0.90 × llm_score + 0.10 × stylo_score
```

Weights in both cases sum to 1.0 (normalized). The LLM signal is weighted higher because it captures semantic intent — the primary distinguishing feature. Short texts reduce stylometric weight because the heuristic metrics require sufficient sample size to be reliable.

---

## Confidence Scoring

The confidence score is a float between 0.0 and 1.0 where **higher = more AI-like**.

| Score Range | Classification | What it means |
|---|---|---|
| 0.00 – 0.34 | Likely Human | Both signals agree on human characteristics |
| 0.35 – 0.64 | Uncertain | Signals disagree or both returned mid-range values |
| 0.65 – 1.00 | Likely AI | Both signals agree on AI characteristics |

Because falsely labeling human work as AI is considered more harmful than missing some AI-generated work, borderline scores are intentionally routed into the wide uncertain band instead of forcing a binary decision.

**Two real example submissions with noticeably different scores (actual output from Milestone 4 testing):**

*High-confidence case* — a formulaic academic-style paragraph ("In conclusion, it is important
to note that the aforementioned factors collectively contribute to a comprehensive
understanding of the subject matter at hand...") produced `llm_score = 0.80`,
`stylo_score = 0.543`, and `final_score = 0.774`. Both signals agree it reads as AI-generated
(formulaic transitions, uniform sentence rhythm), so the score lands solidly in the AI band and
the system commits to the definitive AI label with no hedging.

*Lower-confidence case* — a plain, everyday description ("The weather today was pretty nice,
sunny with a light breeze. I went for a walk in the park and saw several dogs playing...")
produced `llm_score = 0.40`, `stylo_score = 0.514`, and `final_score = 0.411`. The LLM's own
reasoning called this text "moderately likely to be AI-generated" — genuinely torn, because
plain declarative writing is generic enough to plausibly be either. That ambiguity lands the
score inside the uncertain band, and the system reports "Unclear" instead of forcing a guess.

These two cases show the scoring isn't a constant: a text where both signals confidently agree
scores nearly twice as high as one where the LLM itself is unsure, and the combination formula
preserves that difference rather than collapsing it.

The key design choice behind the bands is the wide uncertain range (30 points): it absorbs
borderline cases like the second example above rather than forcing a binary call, reflecting
the asymmetry in harm — misclassifying a human's work as AI is worse than a false negative. The
human (0–0.34) and AI (0.65–1.00) outer bands are roughly symmetric in width (34 vs. 35 points).

**How scores were tested for meaningfulness:** in addition to the two cases above, a personal
journal entry and a short experimental poem were run through both signals individually and
combined, and scores were verified to move in the expected direction and route to the correct
label variant in each case.

---

## Transparency Labels

The label is the text shown directly to a reader on the platform. All three variants are
reproduced verbatim below.

### Variant A — Likely AI-Generated (score ≥ 0.65)

```
Attribution: Likely AI-Generated
Confidence: High

Our system analyzed the writing patterns and style of this piece and found strong
indicators that it was generated with AI assistance. This is not a definitive judgment
— our detection tools are not perfect, and some human writing styles can resemble
AI-generated text.

If you created this work yourself, you can submit an appeal and explain your process.
We will review it and update this label.
```

### Variant B — Likely Human-Written (score ≤ 0.34)

```
Attribution: Likely Human-Written
Confidence: High

Our system analyzed the writing patterns and style of this piece and found strong
indicators that it was written by a person. The language, structure, and style all
appear consistent with human authorship.

If this assessment seems wrong, you can submit an appeal at any time.
```

### Variant C — Unclear / Uncertain (score 0.35 – 0.64)

```
Attribution: Unclear
Confidence: Low

Our system analyzed this piece but could not make a confident determination about
whether it was written by a person or generated with AI assistance. This can happen
with short texts, experimental writing styles, or pieces that blend human and AI work.

No action is taken based on an uncertain result. If you want to provide more context
about how you created this work, you can submit an appeal.
```

---

## Appeals Workflow

Any creator can appeal a classification. Appeals require:
- `content_id`: the ID of the classified piece
- `creator_id`: must match the original submission record (simple string equality check — no authentication is implemented in this version)
- `reasoning`: at least 10 characters explaining how the work was created

On receipt:
1. The system looks up the original decision.
2. Validates `creator_id` against the submission record.
3. Updates content status from `"decided"` to `"under_review"` (decision lifecycle: `decided` → `under_review`).
4. Appends an appeal entry to the audit log with the reasoning and timestamp.
5. Returns confirmation.

The appeal does not trigger automatic re-classification. A human reviewer reads/inspect the full
audit log entry including original scores, label, creator reasoning before deciding the outcome of the appeal.

---

## Rate Limiting

Rate limiting is applied per IP address using Flask-Limiter with an in-memory store.

| Endpoint | Limit | Reasoning |
|---|---|---|
| `POST /submit` | 10 / hour | A real writer submits infrequently. 10/hour covers heavy revision sessions without enabling adversarial probing (resistance to large-scale probing of the classifier: reverse-engineering the classifier needs thousands of samples). |
| `POST /appeal` | 3 / hour | Appeals are deliberate, not bulk actions. 3/hour prevents appeal-spam that could overwhelm a reviewer queue. |
| `GET /log` | 30 / hour | Allows normal reviewer use while preventing automated scraping that could expose patterns useful to adversaries. |

Exceeding a limit returns HTTP 429.

**Verification:** sending 12 rapid requests to `POST /submit` (limit: 10/hour) —
the first 10 return `200`, the rest return `429`:

```
$ for i in $(seq 1 12); do
    curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/submit \
      -H "Content-Type: application/json" \
      -d '{"content": "This is a test submission for rate limit testing purposes only.", "creator_id": "ratelimit-test"}'
  done
200
200
200
200
200
200
200
200
200
200
429
429
```

---

## Audit Log

Every classification decision and appeal is persisted to SQLite in two tables.

**`decisions` table columns:**

| Column | Type | Description |
|---|---|---|
| `content_id` | TEXT PK | Unique ID from the submission |
| `creator_id` | TEXT | Submitter identifier |
| `submitted_at` | TEXT | ISO 8601 timestamp |
| `content_preview` | TEXT | First 200 chars of submitted text |
| `llm_score` | REAL | Raw output of Signal 1 |
| `stylo_score` | REAL | Raw output of Signal 2 |
| `final_score` | REAL | Weighted combined score |
| `attribution` | TEXT | "AI" \| "Human" \| "Uncertain" |
| `label_text` | TEXT | Exact label text shown to the user |
| `status` | TEXT | "decided" \| "under_review" |

**`appeals` table columns:**

| Column | Type | Description |
|---|---|---|
| `appeal_id` | INTEGER PK | Auto-increment |
| `content_id` | TEXT FK | Links to decisions |
| `creator_id` | TEXT | Appealing creator |
| `appealed_at` | TEXT | ISO 8601 timestamp |
| `reasoning` | TEXT | Creator's explanation |

**Sample audit log output** (real output from `GET /log`, run against three test submissions
— `content_preview` and `label_text` are omitted below for brevity; the third entry shows
`status: "under_review"` after its creator filed an appeal):

```json
[
  {
    "content_id": "9e92ba42-d1d4-4c54-af1f-75407b2e5aaf",
    "creator_id": "demo-user-1",
    "submitted_at": "2026-07-01T00:21:16.425173+00:00",
    "llm_score": 0.8,
    "stylo_score": 0.5168627618917853,
    "final_score": 0.7716862761891786,
    "attribution": "AI",
    "status": "decided"
  },
  {
    "content_id": "1caca6ca-f787-4087-bb05-5917c837e02a",
    "creator_id": "demo-user-2",
    "submitted_at": "2026-07-01T00:21:26.497819+00:00",
    "llm_score": 0.2,
    "stylo_score": 0.35585097811810146,
    "final_score": 0.21558509781181018,
    "attribution": "Human",
    "status": "decided"
  },
  {
    "content_id": "02f43342-2e47-4929-8cf8-131907bbca28",
    "creator_id": "demo-user-3",
    "submitted_at": "2026-07-01T00:21:27.247354+00:00",
    "llm_score": 0.8,
    "stylo_score": 0.6219719361479459,
    "final_score": 0.7821971936147947,
    "attribution": "AI",
    "status": "under_review"
  }
]
```

The third entry is a real illustration of the system's documented formal-writing blind spot
(see Known Limitations below): a formal economics paragraph was misclassified as AI-generated,
and the creator's appeal correctly flipped its status to `under_review` pending human review.

---

## Known Limitations

- **Formal human writing** (legal memos, academic papers) can produce false positives because
  it shares structural uniformity with AI text.
- **Short or poetic text** (< 80 words) lacks enough data for reliable stylometric scoring.
  The system reduces stylometric weight and widens the uncertain band for these inputs.
- **Adversarially modified AI text** (prompted to add variance and typos) can partially fool
  the stylometric signal. The LLM signal provides partial defense, but no system is robust
  to deliberate mimicry.

---

## Spec Reflection

**Where the spec helped:** Writing the Transparency Labels section in planning.md before any
code existed meant the exact label text was locked in ahead of time. When it came to
implementation, `label_for()` in [app.py](app.py) is a pure lookup against a dict of strings
copied verbatim from the spec — there was no ambiguity to resolve mid-implementation about
tone or what an "uncertain" result should say to a creator. That upfront decision also made the
asymmetric-harm reasoning (false-positive-AI is worse than false-negative) concrete before
threshold numbers were chosen, so the wide uncertain band in `attribution_for()` is a direct
implementation of a decision made in the spec, not a number picked while coding.

**Where implementation diverged:** planning.md's Audit Log section describes storing the
LLM's `reasoning` string alongside `ai_probability` ("What every entry must preserve... the full
signal breakdown"). During implementation this was dropped — `llm_signal()` in
[signals.py](signals.py) still returns `reasoning`, but `app.py`'s `log_decision()` never
receives or stores it. The reason: the reasoning string is free-form natural-language output
from the LLM, and its wording is not stable across model versions or even temperature settings,
so it isn't something a reviewer could reliably compare across audit log entries. Storing the
numeric score (which is well-defined and comparable) while discarding the prose explanation
gives more consistent long-term auditability at the cost of losing a human-readable rationale
for any single decision — a tradeoff the spec hadn't anticipated when it said to preserve the
"full signal breakdown."

---

## AI Usage

This project was built with Claude Code assistance, using planning.md as the spec that guided
every prompt (see the AI Tool Plan section of [planning.md](planning.md) for what was scoped to
each milestone). Two concrete instances:

1. **Milestone 3 scaffold, later replaced.** I directed Claude to build the initial `/submit`
   endpoint wired only to the LLM signal, before the spec's scoring and label sections were
   implemented. What it produced was intentionally throwaway: a binary threshold
   (`"AI" if ai_probability >= 0.5 else "Human"`) and a hardcoded placeholder string
   (`"We're not sure who wrote this."`) standing in for the real label text. In Milestones 4 and
   5 I directed Claude to replace both — the binary threshold with the three-band
   `attribution_for()` (wide uncertain range, per planning.md's asymmetric-harm reasoning) and
   the placeholder string with the verbatim label text from planning.md's Transparency Labels
   section, copied via `label_for()`. Nothing from the Milestone 3 placeholder logic survived
   into the final version.
2. **Stylometric signal (Milestone 4).** I directed Claude to implement `stylo_signal()` in
   [signals.py](signals.py) directly from the five-metric table in planning.md (sentence-length
   variance, TTR, punctuation density, average word length, paragraph/sentence regularity).
   What it produced matched the spec's formulas on the first pass and needed no functional
   correction — I reviewed each metric against the spec table line-by-line and kept the
   implementation as generated, which is itself a data point: spec sections detailed enough to
   specify the exact statistic and direction of each metric (not just "measure vocabulary
   diversity" but "TTR, lower = more AI-like") produced code that didn't need revising.

---

## Setup

```bash
git clone <repo-url>
cd ai201-project4-provenance-guard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
touch .env   # add your GROQ_API_KEY
python app.py
```

---

## AI Usage

This project was built with Claude Code assistance. The planning.md spec — architecture
diagram, signal design, label variants, scoring thresholds — was written before any
implementation code. That spec was then used as prompt context for generating each milestone.
All generated code was reviewed against the spec before being merged. The transparency label
text and scoring thresholds were designed independently, then implemented.
