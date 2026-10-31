"""Run the summarizer against sample transcripts and check the output.

This calls the real Claude API through the same steps as the app
(app.clean_transcript, then app.summarize), so it costs money: one full run of all cases is
roughly 7 requests (a few cents). Run it before changing SYSTEM_PROMPT,
SUMMARY_SCHEMA or MODEL, and compare the pass rate before and after.

Usage (from mvp-meeting-summarizer/):
    ANTHROPIC_API_KEY=... python eval/run_eval.py
    python eval/run_eval.py --case zoom_vtt_raw --repeat 3
    python eval/run_eval.py --save eval/results.json
    CLAUDE_EFFORT=low python eval/run_eval.py        # compare a lower effort
"""

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CASES_DIR = os.path.join(os.path.dirname(__file__), "cases")

# Per million tokens. Update together with app.MODEL.
PRICE_PER_MTOK = {"claude-sonnet-5": (2.00, 10.00), "claude-sonnet-5-5": (2.00, 10.00)}


def load_cases(only=None):
    cases = []
    for path in sorted(glob.glob(os.path.join(CASES_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        if only and case["name"] not in only:
            continue
        cases.append(case)
    return cases


def _todo_matches(todo, spec):
    if not any(k in todo["task"] for k in spec["keywords"]):
        return False
    if "owner" in spec and spec["owner"] not in todo["owner"]:
        return False
    if "due_contains" in spec:
        options = spec["due_contains"]
        options = [options] if isinstance(options, str) else options
        if not any(o in todo["due"] for o in options):
            return False
    return True


def check(summary, checks):
    """Return a list of failure messages (empty means pass)."""
    if summary is None:
        return ["出力がJSONとして読めなかった"]
    fails = []
    todos, decisions = summary["todos"], summary["decisions"]

    if "todos_min" in checks and len(todos) < checks["todos_min"]:
        fails.append(f"ToDoが少ない: {len(todos)}件 (最低{checks['todos_min']}件)")
    if "todos_max" in checks and len(todos) > checks["todos_max"]:
        fails.append(f"ToDoが多い: {len(todos)}件 (最大{checks['todos_max']}件)")
    if "decisions_min" in checks and len(decisions) < checks["decisions_min"]:
        fails.append(f"決定事項が少ない: {len(decisions)}件 (最低{checks['decisions_min']}件)")
    if "decisions_max" in checks and len(decisions) > checks["decisions_max"]:
        fails.append(f"決定事項が多い: {len(decisions)}件 (最大{checks['decisions_max']}件)")

    for spec in checks.get("todo_contains", []):
        if not any(_todo_matches(t, spec) for t in todos):
            fails.append(f"期待したToDoがない: {spec}")
    for spec in checks.get("todo_must_not_contain", []):
        if any(_todo_matches(t, spec) for t in todos):
            fails.append(f"あってはいけないToDoがある: {spec}")

    allowed = checks.get("allowed_owners")
    if allowed is not None:
        for t in todos:
            if t["owner"] and not any(a in t["owner"] for a in allowed):
                fails.append(f"本文にない担当者: 「{t['owner']}」({t['task']})")
    allowed_dues = checks.get("allowed_dues")
    if allowed_dues is not None:
        for t in todos:
            if t["due"] and not any(a in t["due"] for a in allowed_dues):
                fails.append(f"本文にない期限: 「{t['due']}」({t['task']})")

    for group in checks.get("decision_contains", []):
        if not any(k in d for d in decisions for k in group):
            fails.append(f"期待した決定事項がない: {group}")

    email = summary.get("email", "")
    if (checks.get("email_must_contain") or checks.get("email_must_not_contain")) and not email:
        fails.append("メール下書きがない")
    for group in checks.get("email_must_contain", []):
        if email and not any(k in email for k in group):
            fails.append(f"メールに期待した内容がない: {group}")
    for s in checks.get("email_must_not_contain", []):
        if s in email:
            fails.append(f"メールに本文にない内容「{s}」がある")

    dumped = json.dumps(summary, ensure_ascii=False)
    for s in checks.get("output_must_not_contain", []):
        if s in dumped:
            fails.append(f"出力に「{s}」が含まれている")
    if "summary_min_chars" in checks and len(summary["summary"]) < checks["summary_min_chars"]:
        fails.append(f"要約が短すぎる: {len(summary['summary'])}文字")
    for s in checks.get("summary_must_not_equal", []):
        if summary["summary"].strip() == s:
            fails.append(f"要約が「{s}」だけになっている")
    return fails


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="run only this case (repeatable)")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (output varies run to run)")
    parser.add_argument("--save", help="write every output and result to this JSON file")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY を設定してください(このスクリプトは実際にAPIを呼びます)")

    import app

    cases = load_cases(args.case)
    if not cases:
        sys.exit("該当するケースがありません")

    records, passed, total = [], 0, 0
    in_tok = out_tok = 0
    for case in cases:
        for i in range(args.repeat):
            total += 1
            started = time.time()
            try:
                summary, _, error, message = app.summarize(
                    app.clean_transcript(case["transcript"]).strip(),
                    app.parse_meeting_date(case.get("meeting_date")),
                    case.get("with_email", False),
                )
            except Exception as e:  # report and keep going
                summary, error, message = None, f"{type(e).__name__}: {e}", None
            elapsed = time.time() - started
            if message is not None:
                in_tok += message.usage.input_tokens
                out_tok += message.usage.output_tokens
            fails = [error] if error else check(summary, case["checks"])
            ok = not fails
            passed += ok
            label = case["name"] + (f" #{i + 1}" if args.repeat > 1 else "")
            print(f"{'PASS' if ok else 'FAIL'}  {label}  ({elapsed:.1f}s)")
            for f in fails:
                print(f"      - {f}")
            records.append({"case": case["name"], "run": i + 1, "ok": ok, "fails": fails,
                            "seconds": round(elapsed, 1), "output": summary})

    price_in, price_out = PRICE_PER_MTOK.get(app.MODEL, (0, 0))
    cost = in_tok / 1e6 * price_in + out_tok / 1e6 * price_out
    times = sorted(r["seconds"] for r in records)
    median = times[len(times) // 2] if times else 0
    print(f"\n{passed}/{total} passed  model={app.MODEL} effort={app.EFFORT or 'default'}  "
          f"median={median:.1f}s  "
          f"tokens in={in_tok:,} out={out_tok:,}  cost≈${cost:.3f}")

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump({"model": app.MODEL, "effort": app.EFFORT, "passed": passed, "total": total, "records": records},
                      f, ensure_ascii=False, indent=2)
        print(f"saved: {args.save}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
