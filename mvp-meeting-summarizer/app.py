import csv
import json
import os
import re
import secrets
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
MAX_TRANSCRIPT_CHARS = 50000


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
    return "GJK-" + "".join(secrets.choice(chars) for _ in range(8))


def normalize_code(code):
    return (code or "").strip().upper()


ACCESS_CODE_PATTERN = re.compile(r"^GJK-[A-Z0-9]{8}$")


def find_access_code_by_session(session_id):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["stripe_session_id"] == session_id:
                return row
    return None


def find_access_code_row(code):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["code"] == code:
                return row
    return None


def create_access_code(session_id, uses=PACK_USES, code=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    code = code or generate_access_code()
    is_new_file = not os.path.exists(ACCESS_CODES_FILE)
    with open(ACCESS_CODES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["code", "stripe_session_id", "remaining_uses", "created_at"])
        writer.writerow([code, session_id, uses, datetime.now(timezone.utc).isoformat()])
    return code


# --- Stripe backup of access codes -------------------------------------------
# data/ is wiped on every Render free-plan redeploy, which would make paid codes
# vanish. Each code and its remaining uses are mirrored into the metadata of the
# Stripe PaymentIntent that paid for it, and restored from there on a local miss.


def _metadata(obj):
    md = getattr(obj, "metadata", None)
    if md is None:
        return {}
    return md.to_dict() if hasattr(md, "to_dict") else dict(md)


def _payment_intent_id(session):
    pi = getattr(session, "payment_intent", None)
    if pi is None or isinstance(pi, str):
        return pi
    return getattr(pi, "id", None)


def backup_code_to_stripe(session_id, code, remaining, payment_intent_id=None):
    """Best effort: the local file stays the source of truth while it exists."""
    try:
        if not payment_intent_id:
            payment_intent_id = _payment_intent_id(stripe.checkout.Session.retrieve(session_id))
        if not payment_intent_id:
            return False
        stripe.PaymentIntent.modify(
            payment_intent_id,
            metadata={
                "access_code": code,
                "remaining_uses": str(remaining),
                "checkout_session_id": session_id,
            },
        )
        return True
    except Exception:
        app.logger.exception("Failed to back up access code %s to Stripe", code)
        return False


def restore_code_from_stripe(code):
    """Recreate a local code row from Stripe. Returns True if restored."""
    if not ACCESS_CODE_PATTERN.match(code):
        return False  # also keeps arbitrary input out of the search query
    try:
        results = stripe.PaymentIntent.search(query=f"metadata['access_code']:'{code}'")
    except Exception:
        app.logger.exception("Failed to look up access code %s in Stripe", code)
        return False
    for pi in getattr(results, "data", []):
        md = _metadata(pi)
        if getattr(pi, "status", None) != "succeeded" or md.get("access_code") != code:
            continue
        try:
            remaining = int(md.get("remaining_uses", PACK_USES))
        except ValueError:
            remaining = PACK_USES
        create_access_code(md.get("checkout_session_id", ""), remaining, code=code)
        return True
    return False


def code_has_remaining_uses(code):
    """Check validity without consuming a use."""
    code = normalize_code(code)
    if not code:
        return False
    row = find_access_code_row(code)
    if row is None and restore_code_from_stripe(code):
        row = find_access_code_row(code)
    return row is not None and int(row["remaining_uses"]) > 0


def redeem_code(code):
    """Validate a code and consume one use. Returns True if a use was consumed."""
    code = normalize_code(code)
    if not code or not os.path.exists(ACCESS_CODES_FILE):
        return False
    rows = []
    consumed = None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if consumed is None and row["code"] == code and int(row["remaining_uses"]) > 0:
                row["remaining_uses"] = str(int(row["remaining_uses"]) - 1)
                consumed = row
            rows.append(row)
    if consumed is None:
        return False
    with open(ACCESS_CODES_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    if consumed["stripe_session_id"]:
        backup_code_to_stripe(consumed["stripe_session_id"], code, consumed["remaining_uses"])
    return True


SYSTEM_PROMPT = """あなたは商談・面談の文字起こしから重要情報を抽出するアシスタントです。
入力されたテキストを読み、指定のJSON形式で日本語で出力してください。

- summary: 会話全体の要点を3〜5行でまとめた文章
- decisions: 会話の中で決まったこと。なければ空配列
- todos: 会話の後に誰かがやるべきこと(ToDo)。1件につき1つの具体的な作業にする
  - task: 何をするか(「〜する」で終わる短い文)
  - owner: 担当者。本文から分からなければ空文字
  - due: 期限。本文の表現のまま(例: 「来週金曜」「10/31」)。分からなければ空文字
  ToDoがなければ空配列

本文に書かれていない担当者や期限を推測で埋めないでください。
"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["task", "owner", "due"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "decisions", "todos"],
    "additionalProperties": False,
}


def parse_summary(text):
    """Parse the model's JSON output. Returns None if it isn't the expected shape."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None
    decisions = [d for d in data.get("decisions") or [] if isinstance(d, str) and d.strip()]
    todos = []
    for t in data.get("todos") or []:
        if isinstance(t, dict) and str(t.get("task", "")).strip():
            todos.append(
                {
                    "task": str(t.get("task", "")).strip(),
                    "owner": str(t.get("owner", "")).strip(),
                    "due": str(t.get("due", "")).strip(),
                }
            )
    return {"summary": data["summary"].strip(), "decisions": decisions, "todos": todos}


def summary_to_markdown(s):
    lines = ["## 要約", s["summary"], "", "## 決定事項"]
    lines += [f"- {d}" for d in s["decisions"]] or ["- 特になし"]
    lines += ["", "## ToDo"]
    if not s["todos"]:
        lines.append("- 特になし")
    for t in s["todos"]:
        meta = [f"担当: {t['owner']}" if t["owner"] else "", f"期限: {t['due']}" if t["due"] else ""]
        meta = " / ".join(m for m in meta if m)
        lines.append(f"- [ ] {t['task']}" + (f"({meta})" if meta else ""))
    return "\n".join(lines)


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    summary = None
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
        elif len(transcript) > MAX_TRANSCRIPT_CHARS:
            error = (
                f"テキストが長すぎます({len(transcript):,}文字)。"
                f"{MAX_TRANSCRIPT_CHARS:,}文字以内に分けて貼り付けてください。"
            )
        else:
            allowed = False
            used_code = False
            if access_code:
                if code_has_remaining_uses(access_code):
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
                    # Sonnet 5 thinks by default and thinking tokens count toward
                    # max_tokens, so a small cap truncates the actual answer.
                    message = client.messages.create(
                        model="claude-sonnet-5",
                        max_tokens=16000,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": transcript}],
                        output_config={
                            "format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}
                        },
                    )
                    text_blocks = [
                        block.text for block in message.content if block.type == "text"
                    ]
                    # Check stop_reason first: a refusal or a truncated response
                    # can still carry partial text that must not be shown or billed.
                    if message.stop_reason == "refusal":
                        error = (
                            "入力内容がAIの安全基準により処理を拒否されました。"
                            "内容をご確認のうえ、再度お試しください。"
                        )
                    elif message.stop_reason == "max_tokens" or not text_blocks:
                        error = "AIからの応答を取得できませんでした。もう一度お試しください。"
                    else:
                        raw = "\n".join(text_blocks)
                        summary = parse_summary(raw)
                        result = summary_to_markdown(summary) if summary else raw
                        # Only consume a use once we actually have a result.
                        if used_code:
                            redeem_code(access_code)
                        else:
                            record_free_use(ip)
                except Exception:
                    error = "処理中にエラーが発生しました。時間をおいて再度お試しください。"

        remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(ip)

    return render_template(
        "index.html",
        result=result,
        summary=summary,
        error=error,
        transcript=transcript,
        access_code=access_code,
        remaining_free=max(remaining_free, 0),
        free_limit=FREE_MONTHLY_LIMIT,
        max_chars=MAX_TRANSCRIPT_CHARS,
    )


def csv_safe(value):
    """Stop spreadsheet apps from running user input as a formula when the CSV is opened."""
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


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
                    [
                        datetime.now(timezone.utc).isoformat(),
                        csv_safe(email),
                        csv_safe(role),
                        csv_safe(note),
                    ]
                )
            submitted = True

    return render_template("waitlist.html", submitted=submitted, error=error)


def restore_code_for_session(session):
    """After a redeploy, reloading the success page must show the same code, not mint a new one."""
    pi_id = _payment_intent_id(session)
    if not pi_id:
        return None
    try:
        pi = stripe.PaymentIntent.retrieve(pi_id)
    except Exception:
        app.logger.exception("Failed to retrieve PaymentIntent %s", pi_id)
        return None
    code = _metadata(pi).get("access_code")
    if code and (find_access_code_row(code) or restore_code_from_stripe(code)):
        return code
    return None


def create_and_back_up_code(session_id, session):
    code = create_access_code(session_id)
    backup_code_to_stripe(session_id, code, PACK_USES, _payment_intent_id(session))
    return code


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
    if existing:
        code = existing["code"]
    else:
        code = restore_code_for_session(session) or create_and_back_up_code(session_id, session)

    return render_template("purchase_success.html", error=None, code=code, uses=PACK_USES)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
