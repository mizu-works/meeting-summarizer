"""Automated tests for the meeting-summarizer MVP.

Run with: pytest test_app.py -v

These exist because a production bug (Claude Sonnet 5 returning a
thinking block before the text block) shipped despite being manually
"tested" once. Paid/core-logic code must have real regression coverage,
not ad hoc manual checks. See:
.company/engineering/debug-log/2026-07-29-thinking-block-crash.md
"""

import os
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app as app_module

DATA_DIR = app_module.DATA_DIR


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Every test starts with no persisted usage/code/waitlist data."""
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
    yield
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)


@pytest.fixture(autouse=True)
def no_real_stripe_calls():
    """Default every Stripe call to a harmless mock; tests override as needed."""
    with patch.object(
        app_module.stripe.PaymentIntent, "search", return_value=SimpleNamespace(data=[])
    ) as search, patch.object(app_module.stripe.PaymentIntent, "modify") as modify, patch.object(
        app_module.stripe.PaymentIntent, "retrieve", side_effect=RuntimeError("not mocked")
    ), patch.object(
        app_module.stripe.checkout.Session, "retrieve", side_effect=RuntimeError("not mocked")
    ):
        yield SimpleNamespace(search=search, modify=modify)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def thinking_block():
    return SimpleNamespace(type="thinking", thinking="")


def fake_message(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


SAMPLE_TRANSCRIPT = "田中: 来週の件、決定しました。佐藤: 了解です。"


# --- Core response parsing (the bug that shipped) ---------------------------


def test_thinking_block_before_text_block_still_returns_result(client):
    """Regression test for the production crash: content[0] must not be
    assumed to be the text block."""
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([thinking_block(), text_block("## 要約\nテスト結果")]),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert "テスト結果" in body
    assert "エラー" not in body


def test_text_only_response_still_works(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block("## 要約\nシンプルな結果")]),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert "シンプルな結果" in r.data.decode("utf-8")


def test_refusal_shows_friendly_message_not_crash(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([], stop_reason="refusal"),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert r.status_code == 200
    assert "安全基準" in body


def test_empty_content_non_refusal_shows_friendly_message(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([], stop_reason="max_tokens"),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert r.status_code == 200
    assert "応答を取得できません" in body


def test_api_exception_shows_generic_message_not_raw_exception(client):
    with patch.object(
        app_module.client.messages, "create", side_effect=RuntimeError("boom")
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert r.status_code == 200
    assert "boom" not in body  # never leak raw exception text to users
    assert "エラーが発生しました" in body


# --- Free quota gating --------------------------------------------------


def test_free_quota_allows_up_to_limit_then_blocks(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block("OK")]),
    ):
        for i in range(app_module.FREE_MONTHLY_LIMIT):
            r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
            assert "OK" in r.data.decode("utf-8"), f"use {i + 1} should succeed"

        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
        body = r.data.decode("utf-8")
        assert "上限に達しました" in body


def test_failed_api_call_does_not_consume_free_quota(client):
    with patch.object(
        app_module.client.messages, "create", side_effect=RuntimeError("boom")
    ):
        client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert app_module.count_free_uses_this_month("127.0.0.1") == 0


# --- Paid access codes ---------------------------------------------------


def test_valid_code_bypasses_free_limit_and_is_consumed_on_success(client):
    code = app_module.create_access_code("sess_1", uses=1)
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block("paid result")]),
    ):
        r = client.post(
            "/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": code}
        )
    assert "paid result" in r.data.decode("utf-8")
    assert app_module.code_has_remaining_uses(code) is False


def test_code_not_consumed_when_api_call_fails(client):
    """A paying customer must not lose a use to a transient API error."""
    code = app_module.create_access_code("sess_2", uses=1)
    with patch.object(
        app_module.client.messages, "create", side_effect=RuntimeError("boom")
    ):
        client.post("/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": code})
    assert app_module.code_has_remaining_uses(code) is True


def test_exhausted_code_is_rejected(client):
    code = app_module.create_access_code("sess_3", uses=1)
    app_module.redeem_code(code)  # use it up
    r = client.post(
        "/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": code}
    )
    assert "アクセスコードが無効" in r.data.decode("utf-8")


def test_unknown_code_is_rejected(client):
    r = client.post(
        "/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": "NOT-A-REAL-CODE"}
    )
    assert "アクセスコードが無効" in r.data.decode("utf-8")


# --- /purchase/success ----------------------------------------------------


def test_purchase_success_requires_session_id(client):
    r = client.get("/purchase/success")
    assert "セッション情報が見つかりません" in r.data.decode("utf-8")


def test_purchase_success_handles_stripe_lookup_failure(client):
    with patch.object(
        app_module.stripe.checkout.Session,
        "retrieve",
        side_effect=RuntimeError("network error"),
    ):
        r = client.get("/purchase/success?session_id=sess_bad")
    body = r.data.decode("utf-8")
    assert "network error" not in body
    assert "確認に失敗" in body


def test_purchase_success_rejects_unpaid_session(client):
    with patch.object(
        app_module.stripe.checkout.Session,
        "retrieve",
        return_value=SimpleNamespace(payment_status="unpaid"),
    ):
        r = client.get("/purchase/success?session_id=sess_unpaid")
    assert "お支払いが完了していません" in r.data.decode("utf-8")


def test_purchase_success_issues_code_for_paid_session(client):
    with patch.object(
        app_module.stripe.checkout.Session,
        "retrieve",
        return_value=SimpleNamespace(payment_status="paid"),
    ):
        r = client.get("/purchase/success?session_id=sess_paid_1")
    body = r.data.decode("utf-8")
    assert "GJK-" in body


def test_purchase_success_is_idempotent_for_same_session(client):
    """Reloading the confirmation page must not mint a second code."""
    with patch.object(
        app_module.stripe.checkout.Session,
        "retrieve",
        return_value=SimpleNamespace(payment_status="paid"),
    ):
        r1 = client.get("/purchase/success?session_id=sess_paid_2")
        r2 = client.get("/purchase/success?session_id=sess_paid_2")

    import re

    code1 = re.search(r"GJK-[A-Z0-9]+", r1.data.decode("utf-8")).group()
    code2 = re.search(r"GJK-[A-Z0-9]+", r2.data.decode("utf-8")).group()
    assert code1 == code2


# --- Waitlist --------------------------------------------------------------


def test_waitlist_rejects_invalid_email(client):
    r = client.post("/waitlist", data={"email": "not-an-email"})
    assert "有効なメールアドレス" in r.data.decode("utf-8")


def test_waitlist_accepts_valid_submission(client):
    r = client.post(
        "/waitlist",
        data={"email": "test@example.com", "role": "test", "note": "test"},
    )
    assert "申し込みありがとうございます" in r.data.decode("utf-8")


# --- Static pages -----------------------------------------------------------


def test_privacy_and_terms_pages_load(client):
    assert client.get("/privacy").status_code == 200
    assert client.get("/terms").status_code == 200


# --- Structured summary / ToDo extraction ------------------------------------

import json as _json

STRUCTURED = _json.dumps(
    {
        "summary": "来週の件が決定した。",
        "decisions": ["来週の件を進める"],
        "todos": [
            {"task": "見積書を送付する", "owner": "田中", "due": "来週金曜"},
            {"task": "日程を調整する", "owner": "", "due": ""},
        ],
    },
    ensure_ascii=False,
)


def test_structured_output_renders_todo_table(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([thinking_block(), text_block(STRUCTURED)]),
    ) as create:
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert "見積書を送付する" in body
    assert "来週金曜" in body
    assert "ToDo(2件)" in body
    assert "未定" in body  # missing owner/due shown as 未定, not guessed
    assert "download-csv" in body
    # JSON schema is requested from the API
    assert create.call_args.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_summary_to_markdown_has_checklist():
    s = app_module.parse_summary(STRUCTURED)
    md = app_module.summary_to_markdown(s)
    assert "- [ ] 見積書を送付する(担当: 田中 / 期限: 来週金曜)" in md
    assert "- [ ] 日程を調整する" in md


def test_parse_summary_rejects_non_json():
    assert app_module.parse_summary("## 要約\nただのテキスト") is None
    assert app_module.parse_summary('{"foo": 1}') is None


def test_parse_summary_drops_blank_todos():
    s = app_module.parse_summary(
        _json.dumps({"summary": "x", "decisions": [""], "todos": [{"task": " ", "owner": "", "due": ""}]})
    )
    assert s["decisions"] == [] and s["todos"] == []


def test_empty_todos_shows_tokunashi(client):
    data = _json.dumps({"summary": "雑談のみ", "decisions": [], "todos": []}, ensure_ascii=False)
    with patch.object(
        app_module.client.messages, "create", return_value=fake_message([text_block(data)])
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    body = r.data.decode("utf-8")
    assert "ToDo(0件)" in body
    assert "特になし" in body


def test_model_output_is_html_escaped(client):
    data = _json.dumps(
        {"summary": "<script>alert(1)</script>", "decisions": [], "todos": []}
    )
    with patch.object(
        app_module.client.messages, "create", return_value=fake_message([text_block(data)])
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert "<script>alert(1)</script>" not in r.data.decode("utf-8")


# --- Review fixes -------------------------------------------------------------


def test_truncated_response_is_error_and_not_charged(client):
    """Thinking eats into max_tokens; a cut-off answer must not be shown or billed."""
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block('{"summary": "途中')], stop_reason="max_tokens"),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert "応答を取得できません" in r.data.decode("utf-8")
    assert app_module.count_free_uses_this_month("127.0.0.1") == 0


def test_refusal_with_partial_text_is_not_charged(client):
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block("部分的な")], stop_reason="refusal"),
    ):
        r = client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert "安全基準" in r.data.decode("utf-8")
    assert app_module.count_free_uses_this_month("127.0.0.1") == 0


def test_overlong_transcript_is_rejected_without_api_call(client):
    long_text = "あ" * (app_module.MAX_TRANSCRIPT_CHARS + 1)
    with patch.object(app_module.client.messages, "create") as create:
        r = client.post("/", data={"transcript": long_text})
    assert "長すぎます" in r.data.decode("utf-8")
    create.assert_not_called()


def test_access_code_is_case_insensitive(client):
    code = app_module.create_access_code("sess_case", uses=1)
    assert app_module.code_has_remaining_uses(code.lower())
    assert app_module.redeem_code(" " + code.lower() + " ")
    assert not app_module.code_has_remaining_uses(code)


def test_waitlist_neutralizes_csv_formulas(client):
    client.post(
        "/waitlist",
        data={"email": "a@example.com", "role": "=HYPERLINK(\"x\")", "note": "ok"},
    )
    with open(app_module.WAITLIST_FILE, encoding="utf-8") as f:
        content = f.read()
    assert "'=HYPERLINK" in content


# --- Stripe backup of access codes (survives Render redeploys) ---------------


def paid_session(pi_id="pi_123"):
    return SimpleNamespace(payment_status="paid", payment_intent=pi_id)


def test_new_code_is_backed_up_to_payment_intent(client, no_real_stripe_calls):
    with patch.object(
        app_module.stripe.checkout.Session, "retrieve", return_value=paid_session()
    ), patch.object(
        app_module.stripe.PaymentIntent, "retrieve", return_value=SimpleNamespace(metadata={})
    ):
        r = client.get("/purchase/success?session_id=sess_b1")
    import re

    code = re.search(r"GJK-[A-Z0-9]{8}", r.data.decode("utf-8")).group()
    no_real_stripe_calls.modify.assert_called_once_with(
        "pi_123",
        metadata={
            "access_code": code,
            "remaining_uses": "10",
            "checkout_session_id": "sess_b1",
        },
    )


def test_success_page_after_redeploy_shows_same_code(client, no_real_stripe_calls):
    """data/ was wiped but Stripe still knows the code: don't mint a second one."""
    pi = SimpleNamespace(
        status="succeeded",
        metadata={"access_code": "GJK-ABCD1234", "remaining_uses": "7", "checkout_session_id": "sess_r1"},
    )
    no_real_stripe_calls.search.return_value = SimpleNamespace(data=[pi])
    with patch.object(
        app_module.stripe.checkout.Session, "retrieve", return_value=paid_session()
    ), patch.object(app_module.stripe.PaymentIntent, "retrieve", return_value=pi):
        r = client.get("/purchase/success?session_id=sess_r1")
    assert "GJK-ABCD1234" in r.data.decode("utf-8")
    assert app_module.find_access_code_row("GJK-ABCD1234")["remaining_uses"] == "7"
    no_real_stripe_calls.modify.assert_not_called()


def test_unknown_local_code_is_restored_from_stripe(client, no_real_stripe_calls):
    pi = SimpleNamespace(
        status="succeeded",
        metadata={"access_code": "GJK-REST0RE1", "remaining_uses": "3", "checkout_session_id": "sess_x"},
    )
    no_real_stripe_calls.search.return_value = SimpleNamespace(data=[pi])
    assert app_module.code_has_remaining_uses("gjk-rest0re1")
    no_real_stripe_calls.search.assert_called_once_with(
        query="metadata['access_code']:'GJK-REST0RE1'"
    )
    assert app_module.find_access_code_row("GJK-REST0RE1")["remaining_uses"] == "3"


def test_restored_code_with_zero_uses_is_rejected(client, no_real_stripe_calls):
    pi = SimpleNamespace(
        status="succeeded",
        metadata={"access_code": "GJK-USEDUP00", "remaining_uses": "0", "checkout_session_id": "s"},
    )
    no_real_stripe_calls.search.return_value = SimpleNamespace(data=[pi])
    assert not app_module.code_has_remaining_uses("GJK-USEDUP00")


def test_unpaid_payment_intent_is_not_restored(client, no_real_stripe_calls):
    pi = SimpleNamespace(
        status="requires_payment_method",
        metadata={"access_code": "GJK-NOTPAID0", "remaining_uses": "10"},
    )
    no_real_stripe_calls.search.return_value = SimpleNamespace(data=[pi])
    assert not app_module.code_has_remaining_uses("GJK-NOTPAID0")


def test_malformed_code_never_reaches_stripe_search(client, no_real_stripe_calls):
    assert not app_module.code_has_remaining_uses("GJK-AAAA' OR status:'succeeded")
    no_real_stripe_calls.search.assert_not_called()


def test_stripe_search_failure_just_rejects_code(client, no_real_stripe_calls):
    no_real_stripe_calls.search.side_effect = RuntimeError("stripe down")
    assert not app_module.code_has_remaining_uses("GJK-ABCDEFGH")


def test_redeem_syncs_remaining_uses_to_stripe(client, no_real_stripe_calls):
    code = app_module.create_access_code("sess_sync", uses=5)
    with patch.object(
        app_module.stripe.checkout.Session, "retrieve", return_value=paid_session("pi_sync")
    ):
        assert app_module.redeem_code(code)
    no_real_stripe_calls.modify.assert_called_once_with(
        "pi_sync",
        metadata={"access_code": code, "remaining_uses": "4", "checkout_session_id": "sess_sync"},
    )


def test_redeem_still_works_when_stripe_backup_fails(client, no_real_stripe_calls):
    code = app_module.create_access_code("sess_fail", uses=2)
    no_real_stripe_calls.modify.side_effect = RuntimeError("stripe down")
    with patch.object(
        app_module.stripe.checkout.Session, "retrieve", return_value=paid_session()
    ):
        assert app_module.redeem_code(code)
    assert app_module.find_access_code_row(code)["remaining_uses"] == "1"


def test_stripe_object_metadata_is_read(client):
    """Real StripeObjects are not dicts in stripe-python v16+."""
    pi = app_module.stripe.PaymentIntent.construct_from(
        {"id": "pi_x", "status": "succeeded", "metadata": {"access_code": "GJK-REALOBJ1"}}, "sk_test"
    )
    assert app_module._metadata(pi)["access_code"] == "GJK-REALOBJ1"

def test_form_exposes_char_limit_and_submit_guard(client):
    body = client.get("/").data.decode("utf-8")
    assert f'data-max="{app_module.MAX_TRANSCRIPT_CHARS}"' in body
    assert 'id="submit-button"' in body


# --- Subtitle (.vtt / .srt) cleanup --------------------------------------------

ZOOM_VTT = """WEBVTT

1
00:00:01.200 --> 00:00:05.800
高橋: では定例を始めます。

2
00:00:06.100 --> 00:00:12.400
高橋: デザイン案はAで確定です。

3
00:00:12.900 --> 00:00:18.300
伊藤: はい。
コーディングは私がやります。
"""

TEAMS_VTT = """WEBVTT

0a1b2c3d-4e5f/12-0
00:00:01.000 --> 00:00:04.000
<v 山田 太郎>こんにちは。</v>

0a1b2c3d-4e5f/14-0
00:00:06.500 --> 00:00:08.000
<v 佐藤 花子>資料を共有します。</v>
"""


def test_zoom_vtt_is_cleaned_and_same_speaker_merged():
    assert app_module.clean_transcript(ZOOM_VTT) == (
        "高橋: では定例を始めます。 デザイン案はAで確定です。\n"
        "伊藤: はい。 コーディングは私がやります。"
    )


def test_teams_voice_tags_become_speaker_names():
    assert app_module.clean_transcript(TEAMS_VTT) == (
        "山田 太郎: こんにちは。\n佐藤 花子: 資料を共有します。"
    )


def test_srt_with_crlf_and_no_speakers_keeps_lines():
    srt = "1\r\n00:00:01,000 --> 00:00:03,000\r\nこんにちは\r\n\r\n2\r\n00:00:03,500 --> 00:00:05,000\r\n<i>よろしく</i>\r\n"
    assert app_module.clean_transcript(srt) == "こんにちは\nよろしく"


def test_plain_text_is_left_alone():
    text = "田中: 10:30から会議です。\n佐藤: 了解です。"
    assert app_module.clean_transcript(text) == text


def test_subtitles_are_cleaned_before_length_check_and_api_call(client):
    """A long .vtt that fits once timestamps are gone must be accepted."""
    cue = "00:00:01.000 --> 00:00:02.000\n高橋: はい\n\n"
    vtt = "WEBVTT\n\n" + cue * (app_module.MAX_TRANSCRIPT_CHARS // len(cue) + 10)
    assert len(vtt) > app_module.MAX_TRANSCRIPT_CHARS
    with patch.object(
        app_module.client.messages, "create", return_value=fake_message([text_block("OK")])
    ) as create:
        client.post("/", data={"transcript": vtt})
    sent = create.call_args.kwargs["messages"][0]["content"]
    assert "-->" not in sent
    assert sent.startswith("高橋: はい はい")


# --- Public-facing polish ------------------------------------------------------


def test_oversized_request_shows_japanese_error_page(client):
    big = "あ" * (app_module.app.config["MAX_CONTENT_LENGTH"] // 3 + 10)
    r = client.post("/", data={"transcript": big})
    assert r.status_code == 413
    body = r.data.decode("utf-8")
    assert "ファイルが大きすぎます" in body
    assert 'id="summarize-form"' in body  # user can try again on the same page


def test_index_has_share_metadata(client):
    body = client.get("/").data.decode("utf-8")
    assert 'name="description"' in body
    assert 'property="og:title"' in body
    assert 'content="http://localhost/"' in body


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Allow: /" in r.data.decode("utf-8")


# --- Thread safety (gunicorn runs with --threads) ---------------------------------

import threading as _threading


def _run_concurrently(fn, n):
    barrier = _threading.Barrier(n)
    results = []

    def worker():
        barrier.wait()
        results.append(fn())

    threads = [_threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_redeems_never_exceed_purchased_uses(client):
    code = app_module.create_access_code("sess_conc", uses=10)
    results = _run_concurrently(lambda: app_module.redeem_code(code), 30)
    assert results.count(True) == 10
    assert app_module.find_access_code_row(code)["remaining_uses"] == "0"


def test_new_codes_are_not_lost_while_others_are_redeemed(client):
    """A rewrite during redeem must not drop a row appended at the same moment."""
    code = app_module.create_access_code("sess_busy", uses=100)
    created = []

    def mixed(i=[0]):
        i[0] += 1
        if i[0] % 2:
            return app_module.redeem_code(code)
        created.append(app_module.create_access_code(f"sess_new_{i[0]}"))
        return True

    _run_concurrently(mixed, 40)
    for c in created:
        assert app_module.find_access_code_row(c) is not None


def _slow_ok(**kwargs):
    import time as _time

    _time.sleep(0.2)  # keep requests overlapping
    return fake_message([text_block("OK")])


def test_parallel_free_requests_cannot_exceed_monthly_limit():
    """Several tabs submitted at once must not all slip under the limit."""
    app_module.app.config["TESTING"] = True

    def post():
        c = app_module.app.test_client()
        return "OK" in c.post("/", data={"transcript": SAMPLE_TRANSCRIPT}).data.decode("utf-8")

    with patch.object(app_module.client.messages, "create", side_effect=_slow_ok):
        results = _run_concurrently(post, 12)
    assert results.count(True) == app_module.FREE_MONTHLY_LIMIT
    assert app_module.count_free_uses_this_month("127.0.0.1") == app_module.FREE_MONTHLY_LIMIT


def test_parallel_code_requests_cannot_exceed_purchased_uses():
    app_module.app.config["TESTING"] = True
    code = app_module.create_access_code("sess_par", uses=2)

    def post():
        c = app_module.app.test_client()
        r = c.post("/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": code})
        return "OK" in r.data.decode("utf-8")

    with patch.object(app_module.client.messages, "create", side_effect=_slow_ok):
        results = _run_concurrently(post, 6)
    assert results.count(True) == 2
    assert app_module.find_access_code_row(code)["remaining_uses"] == "0"


def test_reserved_free_use_is_released_on_failure_without_touching_others(client):
    with patch.object(app_module.client.messages, "create", return_value=fake_message([text_block("OK")])):
        client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    with patch.object(app_module.client.messages, "create", side_effect=RuntimeError("boom")):
        client.post("/", data={"transcript": SAMPLE_TRANSCRIPT})
    assert app_module.count_free_uses_this_month("127.0.0.1") == 1


def test_code_use_is_refunded_when_response_is_truncated(client):
    code = app_module.create_access_code("sess_trunc", uses=3)
    with patch.object(
        app_module.client.messages,
        "create",
        return_value=fake_message([text_block("{")], stop_reason="max_tokens"),
    ):
        client.post("/", data={"transcript": SAMPLE_TRANSCRIPT, "access_code": code})
    assert app_module.find_access_code_row(code)["remaining_uses"] == "3"
