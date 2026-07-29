import csv
import os
import random
import string
from datetime import datetime, timezone

import stripe
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

app = Flask(__name__)
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WAITLIST_FILE = os.path.join(DATA_DIR, "waitlist.csv")
ACCESS_CODES_FILE = os.path.join(DATA_DIR, "access_codes.csv")
FREE_USAGE_FILE = os.path.join(DATA_DIR, "free_usage.csv")
FREE_MONTHLY_LIMIT = 5
PACK_USES = 10


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def count_free_uses_this_month(ip):
    if not os.path.exists(FREE_USAGE_FILE):
        return 0
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    count = 0
    with open(FREE_USAGE_FILE, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2 and row[0] == ip and row[1] == month_key:
                count += 1
    return count


def record_free_use(ip):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new_file = not os.path.exists(FREE_USAGE_FILE)
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    with open(FREE_USAGE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["ip", "month", "timestamp"])
        writer.writerow([ip, month_key, datetime.now(timezone.utc).isoformat()])


def generate_access_code():
    chars = string.ascii_uppercase + string.digits
    return "GJK-" + "".join(random.choices(chars, k=8))


def find_access_code_by_session(session_id):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["stripe_session_id"] == session_id:
                return row
    return None


def create_access_code(session_id, uses=PACK_USES):
    os.makedirs(DATA_DIR, exist_ok=True)
    code = generate_access_code()
    is_new_file = not os.path.exists(ACCESS_CODES_FILE)
    with open(ACCESS_CODES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["code", "stripe_session_id", "remaining_uses", "created_at"])
        writer.writerow([code, session_id, uses, datetime.now(timezone.utc).isoformat()])
    return code


def redeem_code(code):
    """Validate a code and consume one use. Returns True if a use was consumed."""
    if not code or not os.path.exists(ACCESS_CODES_FILE):
        return False
    rows = []
    consumed = False
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row["code"] == code.strip() and int(row["remaining_uses"]) > 0:
                row["remaining_uses"] = str(int(row["remaining_uses"]) - 1)
                consumed = True
            rows.append(row)
    if consumed:
        with open(ACCESS_CODES_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return consumed

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
    access_code = ""
    remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(get_client_ip())

    if request.method == "POST":
        transcript = request.form.get("transcript", "").strip()
        access_code = request.form.get("access_code", "").strip()
        ip = get_client_ip()

        if not transcript:
            error = "テキストを貼り付けてください。"
        else:
            allowed = False
            used_code = False
            if access_code:
                if redeem_code(access_code):
                    allowed = True
                    used_code = True
                else:
                    error = "アクセスコードが無効か、利用回数の上限に達しています。"
            elif count_free_uses_this_month(ip) < FREE_MONTHLY_LIMIT:
                allowed = True
            else:
                error = (
                    f"無料枠(月{FREE_MONTHLY_LIMIT}回)の上限に達しました。"
                    "アクセスコードをお持ちの場合は入力してください。"
                )

            if allowed:
                try:
                    message = client.messages.create(
                        model="claude-sonnet-5",
                        max_tokens=1024,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": transcript}],
                    )
                    result = message.content[0].text
                    if not used_code:
                        record_free_use(ip)
                except Exception as exc:
                    error = f"処理中にエラーが発生しました: {exc}"

        remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(ip)

    return render_template(
        "index.html",
        result=result,
        error=error,
        transcript=transcript,
        access_code=access_code,
        remaining_free=max(remaining_free, 0),
        free_limit=FREE_MONTHLY_LIMIT,
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


@app.route("/purchase/success")
def purchase_success():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return render_template(
            "purchase_success.html", error="セッション情報が見つかりません。", code=None
        )

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return render_template(
            "purchase_success.html",
            error="決済情報の確認に失敗しました。時間をおいて再度お試しいただくか、お問い合わせください。",
            code=None,
        )

    if session.payment_status != "paid":
        return render_template(
            "purchase_success.html", error="お支払いが完了していません。", code=None
        )

    existing = find_access_code_by_session(session_id)
    code = existing["code"] if existing else create_access_code(session_id)

    return render_template("purchase_success.html", error=None, code=code, uses=PACK_USES)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
