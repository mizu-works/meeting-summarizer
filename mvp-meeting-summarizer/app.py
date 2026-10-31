import csv
import hmac
import json
import os
import re
import secrets
import string
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import wraps

import stripe
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, Response, abort, render_template, request

load_dotenv()

app = Flask(__name__)
# Generous for a cleaned-up hour-long subtitle file, but stops huge uploads.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
# The SDK default is a 10-minute timeout with 2 retries; nobody waits that long
# in a browser. Fail within a few minutes so the user sees an error and the
# reserved use is given back.
client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=120.0, max_retries=1)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
# Issuing a code calls Stripe while holding DATA_LOCK (so two codes are never
# minted), so a hung Stripe call would stall every request. The library default
# is 80s with 2 retries; cap it at about 20s in total.
stripe.default_http_client = stripe.new_default_http_client(timeout=10)
stripe.max_network_retries = 1

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WAITLIST_FILE = os.path.join(DATA_DIR, "waitlist.csv")
ACCESS_CODES_FILE = os.path.join(DATA_DIR, "access_codes.csv")
FREE_USAGE_FILE = os.path.join(DATA_DIR, "free_usage.csv")
# One row per summarize attempt, for the admin page. Never holds transcript text.
USAGE_LOG_FILE = os.path.join(DATA_DIR, "usage_log.csv")
USAGE_LOG_FIELDS = ["timestamp", "plan", "chars", "seconds", "outcome", "input_tokens", "output_tokens"]
# USD per million tokens (input, output) for the admin page's cost estimate.
MODEL_PRICES = {"claude-sonnet-5": (2.00, 10.00), "claude-sonnet-5-5": (2.00, 10.00)}
FREE_MONTHLY_LIMIT = 5
PACK_USES = 10
MAX_TRANSCRIPT_CHARS = 50000
MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
# How hard the model thinks: low / medium / high / xhigh / max. Unset keeps the
# model default (high on Sonnet 5). Lower is faster and cheaper; compare with
# eval/run_eval.py before changing it in production.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT = os.environ.get("CLAUDE_EFFORT", "").strip().lower() or None
if EFFORT not in (None, *EFFORT_LEVELS):
    app.logger.warning("Ignoring CLAUDE_EFFORT=%r (expected one of %s)", EFFORT, EFFORT_LEVELS)
    EFFORT = None


# gunicorn runs one process with several threads (see render.yaml), so every
# read-modify-write of the CSV files must hold this lock. A second process
# would need a file lock instead.
DATA_LOCK = threading.RLock()


def _locked(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with DATA_LOCK:
            return func(*args, **kwargs)

    return wrapper


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


@_locked
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


@_locked
def record_free_use(ip):
    os.makedirs(DATA_DIR, exist_ok=True)
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    _drop_previous_months(month_key)
    is_new_file = not os.path.exists(FREE_USAGE_FILE)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(FREE_USAGE_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["ip", "month", "timestamp"])
        writer.writerow([ip, month_key, timestamp])
    return timestamp


def _drop_previous_months(month_key):
    """IP addresses are kept only to count this month's free uses (see the
    privacy policy), so older rows are deleted. Rows are appended in time
    order, so checking the first one is enough to know whether to prune."""
    if not os.path.exists(FREE_USAGE_FILE):
        return
    with open(FREE_USAGE_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2 or (len(rows[1]) >= 2 and rows[1][1] == month_key):
        return
    kept = [rows[0]] + [r for r in rows[1:] if len(r) >= 2 and r[1] == month_key]
    with open(FREE_USAGE_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(kept)


@_locked
def reserve_free_use(ip):
    """Take one free use before calling the API, so parallel requests can't
    all pass the limit check. Returns a token for release_free_use, or None."""
    if count_free_uses_this_month(ip) >= FREE_MONTHLY_LIMIT:
        return None
    return record_free_use(ip)


@_locked
def release_free_use(ip, token):
    """Give back a reserved free use when the request didn't produce a result."""
    if not os.path.exists(FREE_USAGE_FILE):
        return
    with open(FREE_USAGE_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    kept = [r for r in rows if not (len(r) >= 3 and r[0] == ip and r[2] == token)]
    with open(FREE_USAGE_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(kept)


# 0/O and 1/I are left out so a code read aloud or copied by hand isn't
# mistyped. Codes issued before this still match ACCESS_CODE_PATTERN.
CODE_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")


def generate_access_code():
    return "GJK-" + "".join(secrets.choice(CODE_CHARS) for _ in range(8))


def normalize_code(code):
    return (code or "").strip().upper()


ACCESS_CODE_PATTERN = re.compile(r"^GJK-[A-Z0-9]{8}$")


@_locked
def find_access_code_by_session(session_id):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["stripe_session_id"] == session_id:
                return row
    return None


@_locked
def find_access_code_row(code):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["code"] == code:
                return row
    return None


@_locked
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
    return any(_restore_from_payment_intent(pi, code) for pi in getattr(results, "data", []))


def _restore_from_payment_intent(pi, code):
    """Recreate the local row for code from a paid PaymentIntent's metadata."""
    md = _metadata(pi)
    if getattr(pi, "status", None) != "succeeded" or md.get("access_code") != code:
        return False
    try:
        remaining = int(md.get("remaining_uses", PACK_USES))
    except ValueError:
        remaining = PACK_USES
    with DATA_LOCK:
        if find_access_code_row(code) is None:
            create_access_code(md.get("checkout_session_id", ""), remaining, code=code)
    return True


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
    with DATA_LOCK:
        consumed = _consume_one_use(code)
    if consumed is None:
        return False
    if consumed["stripe_session_id"]:
        backup_code_to_stripe(consumed["stripe_session_id"], code, consumed["remaining_uses"])
    return True


def refund_code(code):
    """Give back a use taken by redeem_code when the request failed."""
    code = normalize_code(code)
    with DATA_LOCK:
        row = _adjust_uses(code, +1)
    if row is not None and row["stripe_session_id"]:
        backup_code_to_stripe(row["stripe_session_id"], code, row["remaining_uses"])


@_locked
def _adjust_uses(code, delta):
    if not os.path.exists(ACCESS_CODES_FILE):
        return None
    with open(ACCESS_CODES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    target = next((r for r in rows if r["code"] == code), None)
    if target is None:
        return None
    target["remaining_uses"] = str(int(target["remaining_uses"]) + delta)
    with open(ACCESS_CODES_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return target


def _consume_one_use(code):
    if not code or not os.path.exists(ACCESS_CODES_FILE):
        return None
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
    if consumed is not None:
        with open(ACCESS_CODES_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return consumed


TIMESTAMP_LINE = re.compile(r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->")
SPEAKER_LINE = re.compile(r"^([^:：\s][^:：]{0,29})[:：]\s*(.*)$")
VOICE_TAG = re.compile(r"^<v(?:\.[^\s>]*)?\s+([^>]+)>(.*?)(?:</v>)?$")


def clean_transcript(text):
    """Turn Zoom/Teams .vtt or .srt subtitles into plain "name: text" lines.

    Timestamps and cue numbers are dropped and consecutive lines by the same
    speaker are merged. Anything that isn't subtitles is returned unchanged.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not any(TIMESTAMP_LINE.match(line) for line in text.split("\n")):
        return text

    entries = []  # [speaker or None, text]
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        ts = next((i for i, line in enumerate(lines) if TIMESTAMP_LINE.match(line)), None)
        if ts is None:
            continue  # WEBVTT header, NOTE, STYLE, REGION blocks
        for line in lines[ts + 1:]:
            line = line.strip()
            voice = VOICE_TAG.match(line)
            if voice:
                speaker, body = voice.group(1).strip(), voice.group(2)
            else:
                m = SPEAKER_LINE.match(re.sub(r"<[^>]+>", "", line))
                speaker, body = (m.group(1).strip(), m.group(2)) if m else (None, line)
            body = re.sub(r"<[^>]+>", "", body).strip()
            if not body:
                continue
            prev = entries[-1][0] if entries else None
            # Same speaker continues, or an unnamed line continues a named one.
            if entries and prev is not None and speaker in (None, prev):
                entries[-1][1] += " " + body
            else:
                entries.append([speaker, body])
    return "\n".join(f"{sp}: {body}" if sp else body for sp, body in entries)


SYSTEM_PROMPT = """あなたは商談・面談の文字起こしから重要情報を抽出するアシスタントです。
入力されたテキストを読み、指定のJSON形式で日本語で出力してください。

- summary: 会話全体の要点を3〜5行でまとめた文章
- decisions: 会話の中で決まったこと。なければ空配列
- todos: 会話の後に誰かがやるべきこと(ToDo)。1件につき1つの具体的な作業にする
  - task: 何をするか(「〜する」で終わる短い文)
  - owner: 担当者。本文から分からなければ空文字
  - due: 期限。本文の表現のまま(例: 「来週金曜」「10/31」)。分からなければ空文字
    入力の先頭に会議の日付があり、期限が「明日」「来週金曜」「月末」のような相対的な表現なら、
    会議の日付を基準にした具体的な日付を「来週金曜(11/6)」のように括弧で添える
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


EMAIL_INSTRUCTIONS = """
- email: 会議の相手に送る、お礼と確認のメールの下書き(件名を1行目に「件名: 」で書き、本文を続ける)
  - 決定事項と、相手・自分それぞれのToDo(担当・期限)を箇条書きで確認する
  - 丁寧なビジネス文体。宛名と署名は「〇〇様」「(署名)」のように空欄にしておく
  - 本文にない約束・金額・日付を書き足さない
"""

# Same schema plus an "email" field, used only when the user asks for a draft.
SUMMARY_WITH_EMAIL_SCHEMA = {
    **SUMMARY_SCHEMA,
    "properties": {**SUMMARY_SCHEMA["properties"], "email": {"type": "string"}},
    "required": SUMMARY_SCHEMA["required"] + ["email"],
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
    parsed = {"summary": data["summary"].strip(), "decisions": decisions, "todos": todos}
    if isinstance(data.get("email"), str) and data["email"].strip():
        parsed["email"] = data["email"].strip()
    return parsed


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
    if s.get("email"):
        lines += ["", "## メール下書き", s["email"]]
    return "\n".join(lines)


def output_config(with_email=False):
    schema = SUMMARY_WITH_EMAIL_SCHEMA if with_email else SUMMARY_SCHEMA
    config = {"format": {"type": "json_schema", "schema": schema}}
    if EFFORT:
        config["effort"] = EFFORT
    return config


JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = "月火水木金土日"


def parse_meeting_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def build_user_content(transcript, meeting_date=None):
    if meeting_date is None:
        return transcript
    # The weekday is spelled out so the model doesn't have to work it out
    # before counting to "来週金曜".
    label = f"{meeting_date:%Y-%m-%d}({WEEKDAYS_JA[meeting_date.weekday()]})"
    return f"会議の日付: {label}\n\n文字起こし:\n{transcript}"


def summarize(transcript, meeting_date=None, with_email=False):
    """Call Claude and return (summary, markdown, error, message).

    summary is None when the output wasn't the expected JSON; markdown then
    holds the raw text. Exactly one of markdown / error is set.
    """
    # Sonnet 5 thinks by default and thinking tokens count toward
    # max_tokens, so a small cap truncates the actual answer.
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT + EMAIL_INSTRUCTIONS if with_email else SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_content(transcript, meeting_date)}],
        output_config=output_config(with_email),
    )
    text_blocks = [block.text for block in message.content if block.type == "text"]
    # Check stop_reason first: a refusal or a truncated response
    # can still carry partial text that must not be shown or billed.
    if message.stop_reason == "refusal":
        error = (
            "入力内容がAIの安全基準により処理を拒否されました。"
            "内容をご確認のうえ、再度お試しください。"
        )
        return None, None, error, message
    if message.stop_reason == "max_tokens" or not text_blocks:
        return None, None, "AIからの応答を取得できませんでした。もう一度お試しください。", message
    raw = "\n".join(text_blocks)
    summary = parse_summary(raw)
    return summary, (summary_to_markdown(summary) if summary else raw), None, message


def _outcome(message, result, summary):
    if message is None:
        return "error"
    if message.stop_reason == "refusal":
        return "refusal"
    if message.stop_reason == "max_tokens":
        return "truncated"
    if not result:
        return "empty"
    return "ok" if summary else "unparsed"


def log_usage(plan, chars, seconds, outcome, message=None):
    """Never raises: a failed log write must not cost the user their result."""
    try:
        _write_usage_row(plan, chars, seconds, outcome, message)
    except Exception:
        app.logger.exception("Failed to write usage log")


@_locked
def _write_usage_row(plan, chars, seconds, outcome, message):
    usage = getattr(message, "usage", None)
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new_file = not os.path.exists(USAGE_LOG_FILE)
    with open(USAGE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(USAGE_LOG_FIELDS)
        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(),
                plan,
                chars,
                round(seconds, 1),
                outcome,
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            ]
        )


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    summary = None
    error = None
    transcript = ""
    access_code = ""
    meeting_date = datetime.now(JST).date()
    with_email = False
    remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(get_client_ip())

    if request.method == "POST":
        transcript = clean_transcript(request.form.get("transcript", "")).strip()
        access_code = request.form.get("access_code", "").strip()
        meeting_date = parse_meeting_date(request.form.get("meeting_date"))
        with_email = request.form.get("with_email") == "1"
        ip = get_client_ip()

        if not transcript:
            error = "テキストを貼り付けてください。"
        elif len(transcript) > MAX_TRANSCRIPT_CHARS:
            error = (
                f"テキストが長すぎます({len(transcript):,}文字)。"
                f"{MAX_TRANSCRIPT_CHARS:,}文字以内に分けて貼り付けてください。"
            )
        else:
            # Take the use up front (atomically) and give it back on failure;
            # checking first and charging after let parallel requests from
            # several tabs all slip under the limit.
            free_token = None
            used_code = False
            if access_code:
                # code_has_remaining_uses also restores codes wiped by a redeploy
                if code_has_remaining_uses(access_code) and redeem_code(access_code):
                    used_code = True
                else:
                    error = "アクセスコードが無効か、利用回数の上限に達しています。"
            else:
                free_token = reserve_free_use(ip)
                if free_token is None:
                    error = (
                        f"無料枠(月{FREE_MONTHLY_LIMIT}回)の上限に達しました。"
                        "アクセスコードをお持ちの場合は入力してください。"
                    )

            if used_code or free_token:
                started = time.monotonic()
                message = None
                try:
                    summary, result, error, message = summarize(
                        transcript, meeting_date, with_email
                    )
                except Exception:
                    error = "処理中にエラーが発生しました。時間をおいて再度お試しください。"
                log_usage(
                    plan="code" if used_code else "free",
                    chars=len(transcript),
                    seconds=time.monotonic() - started,
                    outcome=_outcome(message, result, summary),
                    message=message,
                )
                if not result:
                    if used_code:
                        refund_code(access_code)
                    else:
                        release_free_use(ip, free_token)

        remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(ip)

    return render_template(
        "index.html",
        result=result,
        summary=summary,
        error=error,
        transcript=transcript,
        access_code=access_code,
        meeting_date=meeting_date.isoformat() if meeting_date else "",
        with_email=with_email,
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
            append_waitlist(email, role, note)
            submitted = True

    return render_template("waitlist.html", submitted=submitted, error=error)


@_locked
def append_waitlist(email, role, note):
    """Add a sign-up; a repeat of an existing email is silently skipped."""
    stored_email = csv_safe(email)
    if any(r["email"].lower() == stored_email.lower() for r in _read_csv(WAITLIST_FILE)):
        return False
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new_file = not os.path.exists(WAITLIST_FILE)
    with open(WAITLIST_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["timestamp", "email", "role", "note"])
        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(),
                stored_email,
                csv_safe(role),
                csv_safe(note),
            ]
        )
    return True


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
    if code and (find_access_code_row(code) or _restore_from_payment_intent(pi, code)):
        return code
    return None


def create_and_back_up_code(session_id, session):
    code = create_access_code(session_id)
    backup_code_to_stripe(session_id, code, PACK_USES, _payment_intent_id(session))
    return code


def ensure_code_for_session(session_id, session):
    """Return the code for a paid Checkout Session, issuing it on first call.

    Called from both the success page and the webhook, in either order and
    any number of times; it is atomic so they never mint two codes.
    """
    with DATA_LOCK:
        existing = find_access_code_by_session(session_id)
        if existing:
            return existing["code"]
        return restore_code_for_session(session) or create_and_back_up_code(session_id, session)


PAID_EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")
# "no_payment_required" is a checkout made free by a 100% coupon (e.g. for testers).
PAID_STATUSES = ("paid", "no_payment_required")


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Issue the code as soon as Stripe confirms payment, so a buyer who
    closes the tab before the success page still has a code on record
    (visible in the Stripe dashboard under the payment's metadata)."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        abort(404)
    try:
        event = stripe.Webhook.construct_event(
            request.get_data(), request.headers.get("Stripe-Signature"), secret
        )
    except Exception:
        return "invalid signature", 400
    if event.type in PAID_EVENTS:
        session = event.data.object
        if getattr(session, "payment_status", None) in PAID_STATUSES:
            ensure_code_for_session(session.id, session)
    return "", 200


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

    if session.payment_status not in PAID_STATUSES:
        return render_template(
            "purchase_success.html", error="お支払いが完了していません。", code=None
        )

    code = ensure_code_for_session(session_id, session)

    return render_template("purchase_success.html", error=None, code=code, uses=PACK_USES)


# Pages whose URL or content must not linger in caches: the success page URL
# carries the Stripe session ID that reveals the access code.
NO_STORE_PREFIXES = ("/purchase/success", "/admin")


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # No framing: stops click-jacking of the access-code form and admin page.
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith(NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def request_too_large(e):
    remaining_free = FREE_MONTHLY_LIMIT - count_free_uses_this_month(get_client_ip())
    return (
        render_template(
            "index.html",
            result=None,
            summary=None,
            error="ファイルが大きすぎます。2MB以下のファイルを使うか、分けて貼り付けてください。",
            transcript="",
            access_code="",
            remaining_free=max(remaining_free, 0),
            free_limit=FREE_MONTHLY_LIMIT,
            max_chars=MAX_TRANSCRIPT_CHARS,
        ),
        413,
    )


# --- Admin page ----------------------------------------------------------------


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with DATA_LOCK, open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _require_admin():
    """404 when no ADMIN_PASSWORD is set, so the page doesn't exist at all."""
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        abort(404)
    auth = request.authorization
    supplied = (auth.password or "") if auth else ""
    if not hmac.compare_digest(supplied.encode(), password.encode()):
        return Response(
            "認証が必要です", 401, {"WWW-Authenticate": 'Basic realm="admin", charset="UTF-8"'}
        )
    return None


def admin_stats(now=None):
    now = now or datetime.now(timezone.utc)
    log = _read_csv(USAGE_LOG_FILE)
    month = now.strftime("%Y-%m")
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

    daily = {d: Counter() for d in days}
    this_month = Counter()
    in_tok = out_tok = 0
    seconds = []
    for row in log:
        day, outcome = row["timestamp"][:10], row["outcome"]
        if day in daily:
            daily[day]["ok" if outcome in ("ok", "unparsed") else "failed"] += 1
            daily[day][row["plan"]] += outcome in ("ok", "unparsed")
        if row["timestamp"][:7] == month:
            this_month[outcome] += 1
            this_month["plan_" + row["plan"]] += outcome in ("ok", "unparsed")
            in_tok += int(row["input_tokens"] or 0)
            out_tok += int(row["output_tokens"] or 0)
            if outcome in ("ok", "unparsed"):
                seconds.append(float(row["seconds"] or 0))

    price_in, price_out = MODEL_PRICES.get(MODEL, (0, 0))
    codes = _read_csv(ACCESS_CODES_FILE)
    seconds.sort()
    return {
        "days": [(d, daily[d]) for d in days],
        "max_daily": max([daily[d]["ok"] + daily[d]["failed"] for d in days] + [1]),
        "month": month,
        "month_counts": this_month,
        "month_total": sum(v for k, v in this_month.items() if not k.startswith("plan_")),
        "tokens_in": in_tok,
        "tokens_out": out_tok,
        "cost_usd": in_tok / 1e6 * price_in + out_tok / 1e6 * price_out,
        "median_seconds": seconds[len(seconds) // 2] if seconds else None,
        "slowest_seconds": seconds[-1] if seconds else None,
        "codes_sold": len(codes),
        "codes_this_month": sum(1 for c in codes if c["created_at"][:7] == month),
        "uses_remaining": sum(int(c["remaining_uses"]) for c in codes),
        "waitlist_count": len(_read_csv(WAITLIST_FILE)),
        "model": MODEL,
        "effort": EFFORT or "既定",
    }


@app.route("/admin")
def admin():
    denied = _require_admin()
    if denied:
        return denied
    return render_template("admin.html", s=admin_stats())


ADMIN_DOWNLOADS = {"waitlist.csv": WAITLIST_FILE, "usage_log.csv": USAGE_LOG_FILE}


@app.route("/admin/download/<name>")
def admin_download(name):
    denied = _require_admin()
    if denied:
        return denied
    path = ADMIN_DOWNLOADS.get(name)
    if path is None or not os.path.exists(path):
        abort(404)
    with DATA_LOCK, open(path, "rb") as f:
        data = f.read()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


@app.route("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\nDisallow: /admin\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
