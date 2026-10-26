"""Deterministic step for the large_loss route: checks the extraction, computes figures.

Called by the pipeline as run(extracted, out_dir) after extracted.json validated
against schema.json. No LLM here (CLAUDE.md rule 1).

Next step: netting through LargeLossesNetting. Pin it in pyproject.toml to a git
tag or commit, call it below, and add package_version("<its distribution name>")
to "versions" so the manifest records exactly which version produced the numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.amounts import check_amount, check_percent, format_amount
from pipeline.evidence import quote_problem

AMOUNTS = ("gross_loss", "previous_estimate", "cash_call")
SOURCED = ("cedent", "broker", "treaty_reference", "underwriting_year", "event_name",
           "date_of_loss", "line_of_business", "country", "cash_call_due_date")
REVIEW_IF_MISSING = ("cedent", "treaty_reference", "date_of_loss")


def run(extracted: dict, out_dir: Path) -> dict:
    job_dir = out_dir.parent.parent  # out_dir is <job>/output/large_loss
    flags: list[str] = []
    facts: dict[str, str] = {}

    # every number must match its verbatim text, and the text must be in the cited file
    for name in AMOUNTS:
        amount = extracted.get(name)
        if amount:
            problem = (check_amount(amount["value"], amount["as_written"], amount["currency"])
                       or quote_problem(job_dir, amount["source"], amount["as_written"]))
            if problem:
                flags.append(f"{name}: {problem}")
    share = extracted.get("our_share")
    if share and (problem := check_percent(share["percent"], share["as_written"])
                  or quote_problem(job_dir, share["source"], share["as_written"])):
        flags.append(f"our_share: {problem}")
    for field, source in extracted.get("sources", {}).items():
        if problem := quote_problem(job_dir, source["file"], source["quote"]):
            flags.append(f"{field}: {problem}")
    unsourced = [f for f in SOURCED if extracted.get(f) is not None and f not in extracted.get("sources", {})]
    if unsourced:
        flags.append("no source quoted for: " + ", ".join(unsourced))
    is_cash_call = extracted.get("notification_type") == "cash_call"
    expected = (*REVIEW_IF_MISSING, "cash_call" if is_cash_call else "gross_loss")
    missing = [f for f in expected if extracted.get(f) is None]
    if missing:
        flags.append("not stated: " + ", ".join(missing))

    gross, previous = extracted.get("gross_loss"), extracted.get("previous_estimate")
    movement = None
    if gross:
        facts["Gross loss (100%)"] = format_amount(gross["value"], gross["currency"])
    if gross and previous:
        if gross["currency"] == previous["currency"]:
            movement = gross["value"] - previous["value"]
            facts["Movement vs previous"] = format_amount(movement, gross["currency"], signed=True)
        else:
            flags.append("gross_loss and previous_estimate are in different currencies: no movement computed")
    if extracted.get("cash_call"):
        due = f" (due {extracted['cash_call_due_date']})" if extracted.get("cash_call_due_date") else ""
        facts["Cash call"] = format_amount(extracted["cash_call"]["value"], extracted["cash_call"]["currency"]) + due
    if share:
        facts["Our share (stated)"] = f"{share['percent']:g}%"
    facts["Netting"] = "not automated yet"

    computed = {"movement_vs_previous": movement, "currency": gross["currency"] if gross else None, "checks": flags}
    (out_dir / "computed.json").write_text(json.dumps(computed, indent=2), encoding="utf-8")
    return {"facts": facts, "flags": flags, "versions": {}}
