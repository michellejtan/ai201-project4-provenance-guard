# Provenance Guard — planning.md

> This document defines the architecture and behavior of the Provenance Guard system.
> It must be completed before implementation.

---

## Architecture

### Architecture Narrative

A piece of text enters the system at `POST /submit`. It is passed to two independent detection
signals that run sequentially: first the LLM classifier (Groq), which evaluates holistic
writing style and semantic coherence, then the stylometric analyzer, which computes measurable
structural statistics. The two raw scores are fed into the confidence scoring engine, which
applies weighted averaging to produce a single 0–1 score. The label generator maps that score
to one of three transparency label variants. Finally, the audit log writer persists the full
decision record to SQLite, and the API returns the result.

An appeal enters at `POST /appeal`. The system looks up the original decision by `content_id`,
appends an appeal entry to the audit log, and flips the content status to `under_review`. No
automatic re-classification happens — a human reviewer reads the audit log later.

### System Flow (Submission)

```
POST /submit  { content, creator_id }
       │
       ▼  generate content_id (server-side UUID)
       │
       ▼  raw text
┌─────────────────────────┐
│  Signal 1: LLM Classifier│  → llm_score (0.0–1.0, higher = more AI-like)
│  (Groq llama-3.3-70b)   │
└─────────────────────────┘
       │
       ▼  raw text
┌─────────────────────────┐
│  Signal 2: Stylometrics  │  → stylo_score (0.0–1.0, higher = more AI-like)
│  (pure Python heuristics)│
└─────────────────────────┘
       │
       ▼  (llm_score, stylo_score)
┌─────────────────────────┐
│  Confidence Scoring      │  → final_score (0.0–1.0)
│  Engine                  │
└─────────────────────────┘
       │
       ▼  final_score
┌─────────────────────────┐
│  Label Generator         │  → label_variant, label_text
└─────────────────────────┘
       │
       ▼  full decision record
┌─────────────────────────┐
│  Audit Log Writer        │  → persisted to SQLite
│  (SQLite)                │
└─────────────────────────┘
       │
       ▼
API Response  { content_id, attribution, confidence, label_text, signals }
```

### Appeal Flow

```
POST /appeal  { content_id, creator_id, reasoning }
       │
       ▼
Lookup content_id in audit log
       │
       ▼  original decision record
Validate creator_id matches submission record → 403 if mismatch
       │
       ▼
Update status: "decided" → "under_review"
       │
       ▼
Append appeal entry to audit log
  { type: "appeal", content_id, creator_id, reasoning, timestamp }
       │
       ▼
API Response  { content_id, status: "under_review", message }
```

---

## Detection Signals

### Signal 1 — LLM Detection (Groq)

**What it measures:** The LLM evaluates holistic writing quality — semantic coherence,
stylistic consistency, naturalness of phrasing, and whether the text "reads" like AI output.
It is the only signal that can catch semantic-level AI tells like perfectly structured
paragraphs, formulaic transitions ("In conclusion…"), and suspiciously balanced arguments.

**Output format:** A JSON object from the LLM containing:
- `ai_probability`: float 0.0–1.0 (higher = more likely AI-generated) — surfaced as `llm_score` in the API response and audit log
- `reasoning`: short string explaining the verdict — used for debugging only; not returned in the API response or stored in the audit log

**Why it can't stand alone:** An LLM classifier is inconsistent on short texts, creative/poetic
styles, and formal academic writing — all of which it may misread as AI. It also has no
structural grounding; it can be fooled by deliberately informal AI output.

**Blind spot:** Formal human writing (legal briefs, academic papers, professional emails) often
scores high on AI-likeness because it shares the same tonal uniformity as AI text.

---

### Signal 2 — Stylometric Heuristics

**What it measures:** Statistical structural properties of the text that differ between human
and AI writing:

| Metric | What it captures | AI pattern |
|---|---|---|
| Sentence length variance (std dev / mean) | Rhythmic consistency | AI = low variance (very uniform) |
| Type-token ratio (TTR) | Vocabulary diversity | AI = slightly lower; humans more repetitive too, but in patterns |
| Punctuation density (punct chars / total chars) | Expressive punctuation | AI = sparse and regular |
| Average word length | Diction register | AI = slightly higher average |
| Paragraph count / sentence count | Structural regularity | AI = very even distribution |

**Output format:** A single `stylo_score` float 0.0–1.0, computed by normalizing and combining
the five metrics. Each metric is mapped to a 0–1 "AI-likeness" sub-score, then averaged.

**Why it can't stand alone:** Short texts (< 80 words) lack enough statistical signal. Poetry
intentionally uses fragmented sentences and unusual spacing that will score as high-AI by
variance metrics. These are structural patterns — they have no semantic understanding.

**Blind spot:** A short poem with simple vocabulary and repetitive structure will score very
high on AI-likeness purely from statistics, even if it's unmistakably human in voice.

---

## Confidence Scoring

### Combination Formula

Normal text (≥ 80 words):
```
final_score = (0.65 × llm_score) + (0.35 × stylo_score)
```

Short text (< 80 words):
```
final_score = (0.90 × llm_score) + (0.10 × stylo_score)
```

Weights in both cases sum to 1.0 (normalized). The LLM signal gets higher weight because it
captures semantic intent, which is the primary distinguishing feature. The stylometric signal
is given near-zero weight on short texts because the heuristics require sufficient sample size
to be reliable.

`final_score` is the internal variable name used throughout this spec and in the audit log. It is returned as `confidence` in the API response.

### Score Interpretation

A score of **0.5** means: both signals gave mixed or contradictory readings. The LLM may have
said "leaning AI" while stylometrics said "leaning human," or both gave near-0.5 outputs
because the text is genuinely ambiguous. A 0.5 should never confidently label either way.

A score of **0.2** means: the LLM saw clear human writing characteristics (varied structure,
idiosyncratic phrasing, emotional register) AND the structural stats match human writing
patterns. High confidence in a human attribution.

A score of **0.85** means: the LLM recognized AI tells AND the statistical profile matches
AI output. High confidence in an AI attribution.

### Threshold Bands

| Score Range | Classification | Label Variant |
|---|---|---|
| 0.00 – 0.34 | Likely Human | Human label |
| 0.35 – 0.64 | Uncertain | Uncertain label |
| 0.65 – 1.00 | Likely AI | AI label |

**Why asymmetric thresholds?** The uncertain band is intentionally wide (30 points) to ensure
the system honestly admits uncertainty rather than forcing a binary call on borderline text.
The human (0–0.34) and AI (0.65–1.00) bands are roughly symmetric in width (34 vs. 35 points);
the design philosophy — that misclassifying a human's work as AI is worse than a false negative
— is enforced by the wide uncertain band absorbing borderline cases, not by making one outer
band wider than the other.

---

## Transparency Labels

All three label variants communicate: what the system found, how confident it is, and
that the creator can appeal. Non-technical language (simple language) throughout.

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

### Variant C — Uncertain (score 0.35 – 0.64)

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

**Who can appeal:** Any creator who submitted the original content (identified by `creator_id`
matching the submission record). No authentication is implemented in this version — `creator_id`
is taken at face value as a string identifier.

**What the creator submits:**
- `content_id` (string): the ID of the classified piece
- `creator_id` (string): must match the original submission
- `reasoning` (string, required): their explanation of how they created the work — minimum
  10 characters enforced. Examples: "I wrote this poem over two weeks in my journal," or
  "This uses my established writing style that I've developed over ten years."

**What the system does:**
1. Looks up the original audit log entry by `content_id`.
2. Validates that `creator_id` matches the submission record.
3. Updates the content status from `"decided"` to `"under_review"`.
4. Appends a new row to the `appeals` table with the creator's reasoning, timestamp,
   and `content_id` linking back to the original decision.
5. Returns a confirmation with the updated status.

**What a human reviewer sees:**
- The original audit log entry: content preview, both signal scores, final score, label assigned, timestamp.
- The appeal entry: creator's reasoning, timestamp.
- The current status: `under_review`.
- The reviewer can manually update status to `"upheld"` (label removed) or `"denied"` (label stands)
  via a future `PATCH /appeal/{content_id}` endpoint (not in scope for this version).

**State machine:**
```
decided → under_review (on appeal submission)
```

---

## Rate Limiting

**Limits chosen:**

| Endpoint | Limit | Window |
|---|---|---|
| `POST /submit` | 10 requests | per hour, per IP |
| `POST /appeal` | 3 requests | per hour, per IP |
| `GET /log` | 30 requests | per hour, per IP |

**Reasoning:**

`POST /submit — 10/hour`: A real writer submits a new piece of work infrequently.
Even a prolific writer revising and resubmitting drafts would rarely need more than 10
submissions per hour. This limit prevents adversarial flooding (probing which phrasings
evade detection) without blocking legitimate use. An adversary trying to reverse-engineer
the classifier would need thousands of probes; 10/hour makes that impractical.

`POST /appeal — 3/hour`: Legitimate appeals are rare and deliberate — a creator appeals
once, not repeatedly. A limit of 3 prevents appeal-spamming to generate log noise or
overwhelm a reviewer queue. It also discourages automated scripts from flooding appeals.

`GET /log — 30/hour`: The audit log endpoint is read-only but could expose patterns
that help adversaries tune their evasion. 30/hour allows normal reviewer use while
preventing automated scraping.

---

## Audit Log

**Storage:** SQLite, two tables: `decisions` and `appeals`.

**`decisions` schema:**
```
content_id     TEXT PRIMARY KEY
creator_id     TEXT
submitted_at   TEXT (ISO 8601)
content_preview TEXT (first 200 chars)
llm_score      REAL
stylo_score    REAL
final_score    REAL
attribution    TEXT  -- "AI" | "Human" | "Uncertain"
label_text     TEXT
status         TEXT  -- "decided" | "under_review"
```

**`appeals` schema:**
```
appeal_id      INTEGER PRIMARY KEY AUTOINCREMENT
content_id     TEXT (FK → decisions.content_id)
creator_id     TEXT
appealed_at    TEXT (ISO 8601)
reasoning      TEXT
```

**What every entry must preserve:**
- The full signal breakdown (both raw scores), so a decision can be reconstructed
  independently of the current model version.
- The label text actually shown to the user (not just the variant name), so there is
  no ambiguity about what the user saw.
- The timestamp, so decisions can be audited chronologically.
- On appeal: the creator's reasoning and a link back to the original decision.

---

## Edge Cases

### Case 1 — Highly formal human writing
**Example:** A legal memo, academic abstract, or business report written by a person.
**Why it fails:** The LLM signal assigns high AI probability because formal human writing
shares tonal uniformity, structured paragraphs, and transition phrases with AI output.
The stylometric signal also flags low sentence-length variance. Both signals cooperate
to produce a false positive.
**Mitigation:** The uncertain band (0.35–0.64) absorbs many of these cases, and the
appeal workflow gives the creator a path to contest it.

### Case 2 — Short or poetic text (< 80 words)
**Example:** A haiku, a short poem, a song lyric fragment.
**Why it fails:** The stylometric signal requires enough sentences to compute meaningful
variance. With fewer than ~5 sentences, standard deviation is unreliable. The TTR on
short texts is artificially high. The signal essentially becomes noise.
**Mitigation:** Add a `min_length` check — if the text is under 80 words, the stylometric
signal is given near-zero weight and the system defaults to a wider uncertain band,
biasing toward the human label when the LLM is also uncertain.

### Case 3 — AI text deliberately written to mimic human variance
**Example:** An adversary prompts an LLM with "write this with uneven sentences,
colloquialisms, and occasional typos."
**Why it fails:** The stylometric signal can be fooled by surface-level variance injection.
The LLM signal may still catch semantic uniformity, but if both fail, the system will
mis-classify as human/bypass detection.
**Mitigation:** This is an acknowledged limitation. No current single classifier is robust
to adversarial mimicry. The system's value is in honest uncertainty communication, not
in being adversarially robust.

---

## AI Tool Plan

### M3 — Submission Endpoint + Signal 1

**Spec sections to provide to AI:** Detection Signals → Signal 1, Architecture diagram
(submission flow), Confidence Scoring (output format of llm_score).

**What to ask for:**
- Flask app skeleton with `POST /submit` accepting `{ content, content_id, creator_id }`.
- A `classify_with_llm(text)` function that calls Groq `llama-3.3-70b-versatile`,
  prompts it to return JSON with `ai_probability` and `reasoning`, and returns the float score.
- The prompt should instruct the model to respond ONLY with valid JSON.

**How to verify:**
- Call the endpoint with a clearly AI-generated paragraph (copy from ChatGPT) → expect
  `llm_score > 0.6`.
- Call with a clearly personal journal entry → expect `llm_score < 0.4`.
- Call with a short poem → observe score and note if it seems calibrated.
- Verify the endpoint returns 422 if `content` is missing.

---

### M4 — Signal 2 + Confidence Scoring

**Spec sections to provide to AI:** Detection Signals → Signal 2 (five metrics table),
Confidence Scoring (formula, threshold bands, score interpretation).

**What to ask for:**
- A `compute_stylometric_score(text)` function implementing all five metrics and returning
  a single 0–1 score.
- The weighted combination: `final_score = 0.65 * llm_score + 0.35 * stylo_score`.
- Integration into the `POST /submit` handler so both scores are computed and combined.
- Edge case: if `word_count < 80`, set `stylo_weight = 0.1` and `llm_weight = 0.9`.

**What to check:**
- Run the same AI paragraph and journal entry through both signals and verify the combined
  score moves in the expected direction.
- Run a short poem → verify the system doesn't confidently classify it.
- Manually check that `final_score = 0.7` routes to AI label and `0.3` routes to human label.

---

### M5 — Production Layer

**Spec sections to provide to AI:** Transparency Labels (all three variant texts),
Appeals Workflow (state machine, input/output), Rate Limiting (limits table),
Audit Log (schema).

**What to ask for:**
- A `generate_label(final_score)` function that maps score to the exact label text from
  this spec (copy the three variants verbatim).
- `POST /appeal` endpoint implementing the full workflow: lookup, creator_id validation,
  status update, audit log append, response.
- Flask-Limiter decorators on `POST /submit` (10/hour) and `POST /appeal` (3/hour).
- SQLite schema creation on app startup and audit log writes on every `POST /submit`.
- `GET /log` endpoint returning the last N audit log entries as JSON.

**How to verify:**
- Submit content → check the audit log has an entry with both signal scores.
- Submit an appeal with matching `creator_id` → check status flips to `under_review`.
- Submit an appeal with wrong `creator_id` → check 403 response.
- Submit 11 requests to `/submit` in rapid succession → check the 11th returns 429.
- Confirm all three label variants appear by submitting text that routes to each score band.
