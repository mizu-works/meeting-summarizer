import csv
import os
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WAITLIST_FILE = os.path.join(DATA_DIR, "waitlist.csv")

SYSTEM_PROMPT = """あなたは商談・面談の文字起こしから重要情報を抽出するアシスタントです。
入力されたテキストを読み、以下の3セクションで日本語で出力してください。他の説明は書かないでください。

## 要約
(3〜5行で会話全体の要点をまとめる)

## 決定事項
- (箇条書き。決定事項がなければ「特になし」)

## Next Action
- (担当者・期限が本文にあれば含めて箇条書き。なければ「特になし」)
"""


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None
    transcript = ""

    if request.method == "POST":
        transcript = request.form.get("transcript", "").strip()
        if not transcript:
            error = "テキストを貼り付けてください。"
        else:
            try:
                message = client.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": transcript}],
                )
                result = message.content[0].text
            except Exception as exc:
                error = f"処理中にエラーが発生しました: {exc}"

    return render_template(
        "index.html", result=result, error=error, transcript=transcript
    )


@app.route("/waitlist", methods=["GET", "POST"])
def waitlist():
    submitted = False
    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        role = request.form.get("role", "").strip()
        note = request.form.get("note", "").strip()

        if "@" not in email or "." not in email.split("@")[-1]:
            error = "有効なメールアドレスを入力してください。"
        else:
            os.makedirs(DATA_DIR, exist_ok=True)
            is_new_file = not os.path.exists(WAITLIST_FILE)
            with open(WAITLIST_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if is_new_file:
                    writer.writerow(["timestamp", "email", "role", "note"])
                writer.writerow(
                    [datetime.now(timezone.utc).isoformat(), email, role, note]
                )
            submitted = True

    return render_template("waitlist.html", submitted=submitted, error=error)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
