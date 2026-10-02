"""Calibrate the Job Applies threshold and relevance constant K against hand-labelled pairs (spec §3.9).

  python -m scripts.job_apply_calibrate labels.csv [--k 0.18] [--llm]

CSV columns: jd (a file path or the JD text itself), resume_path (a .md/.txt resume), verdict (apply|maybe|skip).
Prints the score spread per verdict and the cutoff that best separates "apply" from "skip". Uses the rule-based
extractor by default; --llm uses the LLM extractor (needs OPENAI_API_KEY in the environment).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import statistics
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.job_apply import scoring  # noqa: E402
from app.job_apply.extract import rules_extract  # noqa: E402
from app.job_apply.resume_parse import parse_resume  # noqa: E402
from app.job_apply.sections import substantive_text  # noqa: E402


def _read(value: str) -> str:
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as fh:
            return fh.read()
    return value


async def _extract(jd: str, use_llm: bool, today: date):
    if not use_llm:
        return rules_extract(jd, today)
    from app.job_apply.extract import extract_job

    ex, _ = await extract_job(jd, user=None, run_date=today)
    return ex


def best_cutoff(scored: list[tuple[int, str]]) -> tuple[int, float]:
    labelled = [(s, v) for s, v in scored if v in ("apply", "skip")]
    if not labelled:
        return 75, 0.0
    best = (75, -1.0)
    for cut in range(0, 101):
        right = sum(1 for s, v in labelled if (s >= cut) == (v == "apply"))
        acc = right / len(labelled)
        if acc > best[1] or (acc == best[1] and abs(cut - 75) < abs(best[0] - 75)):
            best = (cut, acc)
    return best


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels")
    ap.add_argument("--k", type=float, default=scoring.K, help="relevance constant K (default %(default)s)")
    ap.add_argument("--llm", action="store_true", help="use the LLM extractor instead of rules")
    args = ap.parse_args()
    scoring.K = args.k
    today = date.today()

    with open(args.labels, newline="", encoding="utf-8-sig") as fh:
        pairs = [(_read(r["jd"]), _read(r["resume_path"]), r["verdict"].strip().lower()) for r in csv.DictReader(fh)]
    if not pairs:
        sys.exit("no rows in labels file")

    idf = scoring.build_idf([substantive_text(jd) or jd for jd, _, _ in pairs])
    scored: list[tuple[int, str]] = []
    for i, (jd, resume, verdict) in enumerate(pairs, 1):
        ex = await _extract(jd, args.llm, today)
        res = scoring.score_resume(
            ex, jd, parse_resume(resume, today), idf,
            run_date=today, account_id="-", resume_id=str(i), filename="-", uploaded_at=None,
        )
        scored.append((res.total, verdict))
        parts = " ".join(f"{k}={c.score:.0f}" for k, c in res.components.items())
        print(f"{i:>3} {verdict:<6} {res.total:>3}  {parts}")

    print("\nverdict  n   min  median  max")
    for verdict in ("apply", "maybe", "skip"):
        vals = [s for s, v in scored if v == verdict]
        if vals:
            print(f"{verdict:<7} {len(vals):>2}  {min(vals):>4}  {statistics.median(vals):>6.1f}  {max(vals):>4}")
    cut, acc = best_cutoff(scored)
    print(f"\nbest cutoff: {cut} (apply-vs-skip accuracy {acc:.0%}) with K={scoring.K}")


if __name__ == "__main__":
    asyncio.run(main())
