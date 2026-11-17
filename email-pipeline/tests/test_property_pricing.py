"""property_pricing: SOV reading, filling the tool's cells, and the checks on what Excel returns.

Excel is replaced by a fake that echoes the writes (tests never need Excel or Claude).
"""
import importlib.util
import json
from datetime import date

import pytest

from conftest import FIXTURES, ROUTES_DIR, job_from
from pipeline import excel
from pipeline.preprocess import preprocess
from pipeline.routes import load_routes
from pipeline.runner import copy_reference, task_prompt

ROUTE = ROUTES_DIR / "property_pricing"
spec = importlib.util.spec_from_file_location("property_pricing_postprocess", ROUTE / "postprocess.py")
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)
CONTRACT = pp.load_contract()


@pytest.fixture
def job(tmp_path):
    job = job_from(tmp_path / "jobs", FIXTURES / "property_quote_request.eml")
    summary = preprocess(job)
    email = summary["emails"][0]
    out = job.output_dir("property_pricing")
    out.mkdir(parents=True)
    return {"job": job, "out": out, "body": email["body"], "sov": email["attachments"][0]["derived"][0]}


def amount(value, text, src, ccy="GBP"):
    return {"value": value, "currency": ccy, "as_written": text, "source": src}


def extraction(body, sov, **changes):
    pct = lambda p, t: {"percent": p, "as_written": t, "source": body}  # noqa: E731
    q = lambda t: {"file": body, "quote": t}  # noqa: E731
    ex = {
        "policy": {"reinsurance_type": "XoL", "new_renewal": "Renewal", "policy_reference": None,
                   "inception_date": "2027-01-01", "expiry_date": None, "quoting_currency": "GBP",
                   "original_insured": "Northwind Foods Ltd", "reinsured": "Example Insurance plc",
                   "reinsured_country": "United Kingdom", "broker": None, "broker_contact": None, "leader": None},
        "coverage": {"pd": "Yes", "bi": "Yes", "flexa_only": None, "mb": None, "cbi": None, "srcc": None},
        "bi": {"mip_months": 12, "waiting_period_days": None, "cbi_mip_months": None, "cbi_waiting_period_days": None},
        "deductions": {"brokerage": pct(15, "15%"), "overrider": None, "fronting_fee": None,
                       "eng_survey_fees": None, "reinsurer_taxes": None, "other_fees": None},
        "layers": [{"layer": "layer_1", "offered_premium": amount(180000, "GBP 180,000", body),
                    "our_share": pct(10, "10%"), "limit": amount(25e6, "GBP 25m", body),
                    "excess": amount(25e6, "GBP 25m", body), "deductible": None, "bi_limit": None,
                    "bi_excess": None, "bi_deductible": None, "eq": None, "ws": None, "fl": None}],
        "risk": {"sector": "Food", "occupancy": "Bakery", "main_risk_country": "United Kingdom"},
        "asset_tables": [{
            "file": sov, "header_cells": ["Site", "Buildings"],
            "columns": {"asset_name": "Site", "country": "Country", "pd_tiv": ["Buildings", "Contents & Stock"],
                        "bi_income": "Gross Profit (BI)", "street_address": "Address"},
            "constants": {"sector": "Food", "occupancy": "Bakery"}, "value_maps": {"country": {"UK": "United Kingdom"}},
            "currency": "GBP", "currency_as_written": "Schedule of values (GBP)", "unit_multiplier": 1,
            "stated_total_pd": None, "stated_total_bi": None}],
        "loss_history": {"years": 5, "losses": [{"date_of_loss": "2024-03-02", "amount": amount(1.2e6, "GBP 1.2m", body),
                                                 "cat": "Non-CAT"}]},
        "sources": {
            "policy.reinsurance_type": q("Layer: GBP 25m xs GBP 25m"), "policy.new_renewal": q("the renewal of"),
            "policy.inception_date": q("12 months at 1 January 2027"), "policy.quoting_currency": q("GBP 25m"),
            "policy.original_insured": q("Northwind Foods Ltd (fictional)"), "policy.reinsured": q("Example Insurance plc"),
            "policy.reinsured_country": q("Example Insurance plc, United Kingdom"),
            "coverage.pd": q("Property Damage"), "coverage.bi": q("Business Interruption"),
            "bi.mip_months": q("BI indemnity period 12 months"), "risk.sector": q("food manufacturer"),
            "risk.occupancy": q("bakery products"), "risk.main_risk_country": q("a UK food manufacturer"),
        },
        "notes": [],
    }
    ex.update(changes)
    return ex


class FakeExcel:
    """Echoes what was written, answers the tool's labels and a few outputs like the real workbook."""

    def __init__(self, label_override=None):
        self.job = None
        self.label_override = label_override or {}

    def __call__(self, job, work_dir):
        self.job = job
        cells = {}
        for w in job.writes:
            cells[(w["sheet"], w["cell"])] = w["value"]
        for t in job.tables:
            col, row = t["cell"][0], int(t["cell"][1:])
            for i, r in enumerate(t["rows"]):
                cells[(t["sheet"], f"{col}{row + i}")] = r[0]
        labels = {(f["sheet"], f["label_cell"]): f["label"] for f in CONTRACT["fields"].values() if f.get("label_cell")}
        labels.update(self.label_override)
        assets = sorted({int(c[1:]) for (s, c) in cells if s == "3_Asset_Details"})
        pd_sum = sum(v for (s, c), v in cells.items() if s == "3_Asset_Details" and c.startswith("G"))
        outs = {"Number_of_Assets": len(assets), "TIV_PD": pd_sum, "Technical_Premium_UW_Total": 150000.0,
                "Technical_Premium_Adequacy_UW_L1": 1.2}

        def read(sheet, cell):
            if (sheet, cell) in labels:
                return {"value": labels[(sheet, cell)], "text": labels[(sheet, cell)]}
            if (sheet, cell) == ("6_Experience_Rating", "B2"):
                return {"value": "Property Rating Tool - v1.08 - Experience", "text": "Property Rating Tool - v1.08 - Experience"}
            if (sheet, cell) in cells:
                v = cells[(sheet, cell)]
                v = v.lstrip("'") if isinstance(v, str) else v
                return {"value": v, "text": str(v)}
            name = next((k.split(".", 1)[1] for k, f in CONTRACT["fields"].items() if (f["sheet"], f["cell"]) == (sheet, cell)), "")
            v = outs.get(name)
            return {"value": v, "text": "" if v is None else str(v)}

        hdr_row = [None] * 23
        for c in CONTRACT["tables"]["3_Asset_Details"]["columns"].values():
            hdr_row[ord(c["column"]) - 65] = c["header"]
        ranges = {}
        for r in job.read_ranges:
            key = f"{r['sheet']}!{r['range']}"
            first, last = r["range"].split(":")
            if r["sheet"] == "DropDownLists":
                ranges[key] = [["Northwind Foods Limited"], ["Other Client plc"], [None]]
            elif first[1:] == last[1:]:
                ranges[key] = [hdr_row]
            else:
                ranges[key] = [[cells.get(("3_Asset_Details", f"{chr(65 + j)}{n}")) for j in range(23)]
                               for n in range(int(first[1:]), int(last[1:]) + 1)]
        return {"ok": True, "excel_version": "16.0", "seconds": 1,
                "reads": {f"{r['sheet']}!{r['cell']}": read(r["sheet"], r["cell"]) for r in job.reads}, "ranges": ranges}


def written(fake, sheet, cell):
    return next(w["value"] for w in fake.job.writes if (w["sheet"], w["cell"]) == (sheet, cell))


def test_route_loads_and_ships_its_vocabulary(tmp_path):
    route = load_routes(ROUTES_DIR)["property_pricing"]
    assert [p.name for p in route.reference] == ["vocabulary.md"]
    assert "./reference/property_pricing/vocabulary.md" in task_prompt(route)
    job = job_from(tmp_path / "jobs", FIXTURES / "property_quote_request.eml")
    copy_reference(job, route)
    assert "Bakery" in (job.dir / "reference" / "property_pricing" / "vocabulary.md").read_text(encoding="utf-8")


def test_fills_the_tool_and_reads_its_results(job):
    fake = FakeExcel()
    result = pp.run(extraction(job["body"], job["sov"]), job["out"], today=date(2026, 11, 17), excel_run=fake)
    assert result["flags"] == []
    assert written(fake, "1_Policy_Information", "D57") == 25e6          # Limit_PD_L1
    assert written(fake, "1_Policy_Information", "D65") == 25e6          # Excess_PD_L1
    assert written(fake, "1_Policy_Information", "D59") == 25e6          # EQ: no sublimit -> layer limit
    assert written(fake, "7_Summary", "E28") == pytest.approx(0.10)      # our share, layer 1
    assert written(fake, "1_Policy_Information", "C42") == pytest.approx(0.15)
    assert written(fake, "1_Policy_Information", "C14") == excel.excel_date("2027-01-01")
    tables = {t["cell"]: [r[0] for r in t["rows"]] for t in fake.job.tables}
    assert tables["G7"] == [15e6, 9e6, 9e6]                              # buildings + contents, totals row left out
    assert tables["O7"] == ["United Kingdom"] * 3                        # value map applied
    assert tables["I7"] == [12] and tables["I9"] == [12] and "I8" not in tables  # policy MIP only where there is BI
    assert not any(c[0] in "ABJLMNPTU" for c in tables)                  # the tool's own formulas stay
    assert tables["C20"] == [excel.excel_date("2024-03-02")] and tables["D20"] == [1.2e6]
    assert result["facts"]["Assets"].startswith("3 (PD GBP 33,000,000")
    assert result["facts"]["Adequacy UW (tool)"] == "L1 120%"
    assert "insured 'Northwind Foods Ltd' is not in the tool's list (closest: Northwind Foods Limited" in result["facts"]["Notes"]
    assert "orig_insured" not in CONTRACT["lists"]  # client names never live in git
    computed = json.loads((job["out"] / "computed.json").read_text(encoding="utf-8"))
    assert computed["asset_tables"][0]["reconciled_with_totals_row"] == ["Buildings", "Contents & Stock", "Gross Profit (BI)"]


def test_wrong_figures_and_unknown_values_are_flagged(job):
    ex = extraction(job["body"], job["sov"])
    ex["layers"][0]["limit"] = amount(2.5e6, "GBP 25m", job["body"])     # mis-scaled
    ex["risk"]["occupancy"] = "Bakeries"                                   # not in the tool's list
    ex["asset_tables"][0]["value_maps"] = {}                               # 'UK' is not a tool country
    flags = pp.run(ex, job["out"], today=date(2026, 11, 17), excel_run=FakeExcel())["flags"]
    assert any(f.startswith("layers.0.limit:") for f in flags)
    assert any("occupancy for sector Food 'Bakeries'" in f and "Bakery" in f for f in flags)
    assert any("country 'UK' not in the tool's list" in f for f in flags)
    assert any("have no country" in f for f in flags)


def test_assets_without_sector_take_the_main_risk(job):
    ex = extraction(job["body"], job["sov"])
    ex["asset_tables"][0]["constants"] = {}
    fake = FakeExcel()
    result = pp.run(ex, job["out"], today=date(2026, 11, 17), excel_run=fake)
    tables = {t["cell"]: [r[0] for r in t["rows"]] for t in fake.job.tables}
    assert tables["E7"] == ["Food"] * 3 and tables["F7"] == ["Bakery"] * 3
    assert result["flags"] == [] and "given the main risk's: Food" in result["facts"]["Notes"]


def test_sov_in_another_currency_is_not_written(job):
    ex = extraction(job["body"], job["sov"])
    ex["asset_tables"][0]["currency"] = "EUR"
    fake = FakeExcel()
    flags = pp.run(ex, job["out"], today=date(2026, 11, 17), excel_run=fake)["flags"]
    assert any("is in EUR, the quote in GBP" in f for f in flags)
    assert not any(t["sheet"] == "3_Asset_Details" for t in fake.job.tables)


def test_totals_row_that_does_not_add_up_is_flagged(job):
    sov = job["job"].dir / job["sov"]
    sov.write_text(sov.read_text(encoding="utf-8").replace("26500000", "26000000"), encoding="utf-8")
    flags = pp.run(extraction(job["body"], job["sov"]), job["out"], today=date(2026, 11, 17), excel_run=FakeExcel())["flags"]
    assert any("'Buildings' adds up to 26,500,000, its totals row says 26,000,000" in f for f in flags)


def test_a_changed_tool_layout_stops_the_run(job):
    moved = {("1_Policy_Information", CONTRACT["fields"]["1_Policy_Information.Brokerage"]["label_cell"]): "Overrider"}
    with pytest.raises(RuntimeError, match="layout differs"):
        pp.run(extraction(job["body"], job["sov"]), job["out"], today=date(2026, 11, 17), excel_run=FakeExcel(moved))


def test_excel_helpers():
    assert excel.excel_date("2027-01-01") == 46388.0
    assert excel.as_text("00123") == "'00123" and excel.as_text("Leeds") == "Leeds" and excel.as_text("=1+1") == "'=1+1"
