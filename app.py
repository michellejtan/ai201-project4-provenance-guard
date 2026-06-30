import uuid
from flask import Flask, request, jsonify

from signals import llm_signal

app = Flask(__name__)

@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json()

    content = data.get("content")
    creator_id = data.get("creator_id")

    content_id = str(uuid.uuid4()) # create once

    llm_result = llm_signal(content)
    attribution = "AI" if llm_result["ai_probability"] >= 0.5 else "Human"

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": 0.5,  # placeholder until Milestone 4's combined scoring
        "label": "We're not sure who wrote this.",  # placeholder until Milestone 5's label generator
})

@app.route("/")
def home():
    return "Provenance Guard is running."

if __name__ == "__main__":
    app.run(port=5000, debug=True)