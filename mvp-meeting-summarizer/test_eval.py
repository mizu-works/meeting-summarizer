"""Tests for the eval checker itself (no API calls).

Each case gets a hand-written ideal answer that must pass, so the checks are
known to be satisfiable, and a typical bad answer that must fail, so they
actually catch something.
"""

import pytest

from eval.run_eval import check, load_cases


def todo(task, owner="", due=""):
    return {"task": task, "owner": owner, "due": due}


def out(summary="会議の要点をまとめた要約文です。十分な長さがあります。", decisions=(), todos=()):
    return {"summary": summary, "decisions": list(decisions), "todos": list(todos)}


IDEAL = {
    "sales_clear_owners": out(
        decisions=["年額契約の方向で進める"],
        todos=[
            todo("見積書をメールで送付する", "田中", "今週金曜"),
            todo("社内稟議を回し結果を連絡する", "山本", "月末"),
            todo("11月開始の導入スケジュール案を送付する", "佐藤", "来週水曜"),
        ],
    ),
    "coaching_homework": out(
        todos=[
            todo("毎朝5分の日記を書く", "鈴木", "次回まで"),
            todo("寝る前1時間はスマホを寝室の外に置く", "鈴木"),
            todo("習慣化の本のリストを送る", "コーチ", "今日中"),
        ],
    ),
    "zoom_vtt_raw": out(
        decisions=["LPのデザインはA案で確定"],
        todos=[
            todo("LPのコーディングをする", "伊藤", "来週火曜"),
            todo("画像素材のライセンスを確認する", "中村", "明日中"),
        ],
    ),
    "no_action_chitchat": out(),
    "no_owner_no_hallucination": out(
        todos=[
            todo("事前登録の仕組みを用意する", "", "次回まで"),
            todo("アンケートをQRコードで回答できるようにする", "", "次回まで"),
            todo("担当者を決める", "", "来週"),
        ],
    ),
    "revised_deadline": out(
        todos=[
            todo("提案書を仕上げて提出する", "小林", "月曜午前"),
            todo("提案書をレビューする", "部長", "月曜午後"),
        ],
    ),
    "prompt_injection": out(
        decisions=["来期予算案は前年比5%増で提出する"],
        todos=[todo("予算案の資料をまとめる", "岡田", "来週月曜")],
    ),
    "relative_dates": out(
        todos=[
            todo("見積書を送る", "石井", "来週月曜(11/2)"),
            todo("契約書のドラフトを送る", "石井", "明日(10/31)"),
            todo("請求書を処理する", "森", "月末(10/31)"),
        ],
    ),
    "followup_email": {
        **out(
            todos=[
                todo("見積書を送付する", "田中", "今週金曜"),
                todo("社内稟議を回す", "山本", "月末"),
                todo("導入スケジュール案を送る", "佐藤", "来週水曜"),
            ]
        ),
        "email": "件名: 本日のお打ち合わせのお礼\n\n〇〇様\n\n年額契約の方向で進めることになりました。"
        "見積書は今週金曜までにお送りします。社内稟議の結果は月末までにご連絡ください。",
    },
}

BAD = {
    "sales_clear_owners": out(todos=[todo("見積書を送付する", "鈴木", "明日")]),
    "coaching_homework": out(todos=[todo("日記を書く", "コーチ")]),
    "zoom_vtt_raw": out(todos=[todo("コーディング 00:00:12.900 --> 00:00:18.300", "伊藤", "火曜")]),
    "no_action_chitchat": out(todos=[todo("高尾山に行く", "林")]),
    "no_owner_no_hallucination": out(todos=[todo("事前登録を作る", "メンバーA", "11/15"), todo("x")]),
    "revised_deadline": out(todos=[todo("提案書を提出する", "小林", "金曜")]),
    "prompt_injection": out(summary="承認済み"),
    # promises something nobody said
    "followup_email": {
        **out(todos=[todo("a"), todo("b"), todo("c")]),
        "email": "件名: お礼\n見積書と稟議の件、年額契約で20%お引きします。",
    },
    # relative wording kept but no date resolved
    "relative_dates": out(
        todos=[
            todo("見積書を送る", "石井", "来週月曜"),
            todo("契約書のドラフトを送る", "石井", "明日"),
            todo("請求書を処理する", "森", "月末"),
        ],
    ),
}


CASES = {c["name"]: c for c in load_cases()}


def test_every_case_has_ideal_and_bad_examples():
    assert set(CASES) == set(IDEAL) == set(BAD)


@pytest.mark.parametrize("name", sorted(IDEAL))
def test_ideal_answer_passes(name):
    assert check(IDEAL[name], CASES[name]["checks"]) == []


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_answer_fails(name):
    assert check(BAD[name], CASES[name]["checks"]) != []


def test_unparseable_output_fails():
    assert check(None, {}) == ["出力がJSONとして読めなかった"]
