"""Deterministic step for property_pricing: checks the extraction, fills the Property
Pricing Tool with it, lets Excel calculate, and reads the tool's results back.

No LLM and no actuarial arithmetic here (CLAUDE.md rule 1): this code copies the
extracted inputs into the tool's input cells (tool/contract.json says where), reads
the schedule of values the agent pointed to, and checks the tool's own outputs
against what was written. The pricing itself is the tool's.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path

from pipeline import excel
from pipeline.amounts import amount_readings, check_amount, check_percent, format_amount
from pipeline.evidence import quote_problem
from pipeline.guard import path_problem
from pipeline.jobs import safe_name

TOOL_DIR = Path(__file__).parent / "tool"
POLICY, RISK, ASSETS, EXPERIENCE, SUMMARY, PRICING = (
    "1_Policy_Information", "2_Risk_Information", "3_Asset_Details", "6_Experience_Rating", "7_Summary", "8_Pricing_Details")
LAYERS = {"primary": "Primary", "layer_1": "L1", "layer_2": "L2", "layer_3": "L3", "layer_4": "L4", "layer_5": "L5"}
COVERAGE = {"pd": "Physical_Damage_PD", "bi": "Business_Interruption_BI", "flexa_only": "Fire_Lightning_Explosion_and_Aircraft_FLEXA_Only",
            "mb": "Machinery_Breakdown_MB", "cbi": "Contingent_Business_Interruption_CBI", "srcc": "Strikes_Riots_and_Civil_Comotion_SRCC"}
DEDUCTIONS = {"brokerage": "Brokerage", "overrider": "Overrider", "fronting_fee": "Fronting_Fee",
              "eng_survey_fees": "Eng_Survey_Costs_Fees", "reinsurer_taxes": "Reinsurer_Payable_Taxes", "other_fees": "Other_Fees_NCD"}
POLICY_TEXT = {"policy_reference": "Policy_Reference", "original_insured": "Original_Insured", "reinsured": "Reinsured",
               "reinsured_country": "Reinsured_Country", "broker": "Broker", "broker_contact": "Broker_Contact", "leader": "Leader"}
# asset table: tool column per extracted field (3_Asset_Details); A (asset id) is numbered here
ASSET_COLUMNS = {"asset_name": "Asset_Name", "sector": "Sector", "occupancy": "Occupancy", "pd_tiv": "Asset_TIV_PD",
                 "bi_income": "Insured_Income_BI", "bi_mip_months": "BI_MIP_Months", "bi_waiting_period_days": "BI_Waiting_Period_Days",
                 "country": "Asset_Risk_Country", "cat_location": "Asset_Risk_Location", "street_address": "Location_Street_Address",
                 "latitude": "Latitude", "longitude": "Longitude", "construction_type": "Construction_Type",
                 "sprinkler": "Sprinkler", "year_of_build": "Year_of_Build"}
NUMERIC = {"pd_tiv", "bi_income", "bi_mip_months", "bi_waiting_period_days", "latitude", "longitude", "year_of_build"}
CUSTOM_LOCATIONS = [f"Custom {i:02d}" for i in range(1, 21)]
MAX_ROWS = {"assets": 6895, "losses": 200}


def load_contract() -> dict:
    return json.loads((TOOL_DIR / "contract.json").read_text(encoding="utf-8"))


def _norm(text) -> str:
    return " ".join(str(text).replace("\xa0", " ").split()).casefold()


def _closest(value: str, options: list) -> str:
    near = difflib.get_close_matches(str(value), [str(o) for o in options], n=3, cutoff=0.5)
    return f" (closest: {', '.join(near)})" if near else ""


class Filler:
    """Collects the cells to write, checking each value against the tool's lists."""

    def __init__(self, contract: dict):
        self.c = contract
        self.writes: dict[str, tuple[str, str, object]] = {}  # field key -> (sheet, cell, value)
        self.tables: list[dict] = []
        self.flags: list[str] = []
        self.info: list[str] = []

    def field(self, sheet: str, name: str) -> dict:
        return self.c["fields"][f"{sheet}.{name}"]

    def put(self, sheet: str, name: str, value) -> None:
        f = self.field(sheet, name)
        if f["direction"] != "IN":
            raise ValueError(f"{sheet}.{name} is an output of the tool, not an input")
        if value is None:
            return
        if f["type"] == "string":
            value = excel.as_text(str(value))
        self.writes[f"{sheet}.{name}"] = (sheet, f["cell"], value)

    def listed(self, what: str, value, options: list, strict: bool = True):
        """value if it is in the tool's list; else None (strict) or value, with a note."""
        if value is None:
            return None
        by_norm = {_norm(o): o for o in options}
        if _norm(value) in by_norm:
            return by_norm[_norm(value)]
        message = f"{what} '{value}' is not in the tool's list{_closest(value, options)}"
        if strict:
            self.flags.append(message + ": left empty")
            return None
        self.info.append(message)
        return value

    def column(self, sheet: str, col: str, start_row: int, values: list) -> None:
        """Write a column of a table in runs, skipping None so the tool's default formulas stay."""
        run_start, run = None, []
        for i, v in enumerate([*values, None]):
            if v is not None:
                if run_start is None:
                    run_start = i
                run.append([v])
            elif run:
                self.tables.append({"sheet": sheet, "cell": f"{col}{start_row + run_start}", "rows": run})
                run_start, run = None, []


# ---------------------------------------------------------------- checks on the extraction

def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}.{i}")


def check_evidence(extracted: dict, job_dir: Path) -> list[str]:
    """Every amount/percent must read as written and be in its source; every quote too."""
    flags = []
    for path, node in _walk(extracted):
        if path.startswith("sources"):
            continue
        if {"value", "as_written", "source"} <= node.keys():
            problem = (check_amount(node["value"], node["as_written"], node.get("currency"))
                       or quote_problem(job_dir, node["source"], node["as_written"]))
        elif {"percent", "as_written", "source"} <= node.keys():
            problem = (check_percent(node["percent"], node["as_written"])
                       or quote_problem(job_dir, node["source"], node["as_written"]))
        else:
            continue
        if problem:
            flags.append(f"{path}: {problem}")
    for name, src in (extracted.get("sources") or {}).items():
        if problem := quote_problem(job_dir, src["file"], src["quote"]):
            flags.append(f"{name}: {problem}")
    sources = extracted.get("sources") or {}
    unsourced = [f"{group}.{k}" for group in ("policy", "coverage", "bi", "risk")
                 for k, v in (extracted.get(group) or {}).items() if v is not None and f"{group}.{k}" not in sources]
    if unsourced:
        flags.append("no source quoted for: " + ", ".join(unsourced))
    return flags


# ---------------------------------------------------------------- schedule of values

def parse_number(text: str):
    """A cell's number, or None if empty; raises ValueError if it does not read as one number."""
    text = str(text).strip()
    if text in ("", "-", "nan", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        pass
    readings = amount_readings(text)
    if len(readings) != 1:
        raise ValueError(text)
    return readings[0]


def _is_number(text: str) -> bool:
    try:
        return parse_number(text) is not None
    except ValueError:
        return False


def read_asset_table(spec: dict, job_dir: Path, lists: dict, quoting_currency: str | None) -> tuple[list[dict], list[str], dict]:
    """Rows of the tool's asset table from the CSV the agent pointed to. Returns (rows, flags, stats)."""
    flags, stats = [], {"file": spec["file"]}
    if problem := path_problem(spec["file"], job_dir.resolve(), job_dir.resolve()):
        return [], [f"asset table {spec['file']}: {problem}"], stats
    path = (job_dir / spec["file"]).resolve()
    if not path.is_file():
        return [], [f"asset table {spec['file']} does not exist"], stats
    if spec.get("currency") and quoting_currency and spec["currency"] != quoting_currency:
        return [], [f"asset table {spec['file']} is in {spec['currency']}, the quote in {quoting_currency}: not written "
                    "(the tool needs values in the quoting currency)"], stats
    if not spec.get("currency"):
        flags.append(f"asset table {spec['file']}: currency not stated, assumed to be the quoting currency")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        table = list(csv.reader(fh))
    wanted = [_norm(h) for h in spec["header_cells"]]
    header_idx = next((i for i, row in enumerate(table) if all(w in {_norm(c) for c in row} for w in wanted)), None)
    if header_idx is None:
        return [], [f"asset table {spec['file']}: no row contains the header cells {spec['header_cells']}"], stats
    header = [_norm(c) for c in table[header_idx]]
    stats["header_row"] = header_idx + 1

    cols: dict[str, list[int]] = {}
    for key, source in (spec.get("columns") or {}).items():
        if source is None:
            continue
        names = source if isinstance(source, list) else [source]
        idx = []
        for n in names:
            if _norm(n) not in header:
                flags.append(f"asset table: column '{n}' (for {key}) not found in the header row")
                continue
            if header.count(_norm(n)) > 1:
                flags.append(f"asset table: several columns are headed '{n}'; the first one was used for {key}")
            idx.append(header.index(_norm(n)))
        if idx:
            cols[key] = idx
    if "pd_tiv" not in cols and "bi_income" not in cols:
        return [], flags + [f"asset table {spec['file']}: no PD or BI value column found: not written"], stats

    constants, maps = spec.get("constants") or {}, spec.get("value_maps") or {}
    mult = spec.get("unit_multiplier") or 1
    rows, totals_skipped, bad_numbers, bad_values = [], 0, [], {}
    value_cols = {i for k in ("pd_tiv", "bi_income") for i in cols.get(k, [])}
    column_sums: dict[int, list[float]] = {i: [] for i in value_cols}  # source column -> values of the rows kept
    total_rows: list[list[str]] = []
    text_keys = [k for k in cols if k not in NUMERIC]
    for line_no, raw in enumerate(table[header_idx + 1:], start=header_idx + 2):
        cell = lambda i: raw[i] if i < len(raw) else ""  # noqa: E731
        if all(not cell(i).strip() for idx in cols.values() for i in idx):
            continue
        if any(re.search(r"\btotals?\b", c, re.IGNORECASE) for c in raw[:max(i for idx in cols.values() for i in idx) + 1]
               if not _is_number(c)):
            totals_skipped += 1
            total_rows.append(raw)
            continue
        row = {"line": line_no}
        for key, idx in cols.items():
            if key in NUMERIC:
                parts = []
                for i in idx:
                    try:
                        parts.append(parse_number(cell(i)))
                    except ValueError:
                        bad_numbers.append(f"line {line_no} '{cell(i)}'")
                parts = [p for p in parts if p is not None]
                value = sum(parts) if parts else None  # several source columns add up to one tool column
                if value is not None and key in ("pd_tiv", "bi_income"):
                    value *= mult
                row[key] = value
            else:
                row[key] = " ".join(cell(i).strip() for i in idx).strip() or None
        for key, value in constants.items():
            row.setdefault(key, value)
            if row[key] is None:
                row[key] = value
        for key, mapping in maps.items():
            if row.get(key) is not None:
                by_norm = {_norm(k): v for k, v in mapping.items()}
                row[key] = by_norm.get(_norm(row[key]), row[key])
        if row.get("pd_tiv") is None and row.get("bi_income") is None and not row.get("asset_name"):
            continue
        for i in value_cols:
            try:
                column_sums[i].append(parse_number(cell(i)) or 0.0)
            except ValueError:
                pass
        _check_row(row, lists, bad_values)
        rows.append(row)

    stats["total_rows_skipped"] = totals_skipped
    if len(total_rows) == 1:  # a single totals row: every value column must add up to it
        reconciled = []
        for i in sorted(value_cols):
            try:
                stated = parse_number(total_rows[0][i] if i < len(total_rows[0]) else "")
            except ValueError:
                stated = None
            if stated is None:
                continue
            got = math.fsum(column_sums[i])
            if math.isclose(got, stated, rel_tol=1e-9, abs_tol=0.5):
                reconciled.append(table[header_idx][i])
            else:
                flags.append(f"asset table: column '{table[header_idx][i]}' adds up to {got:,.0f}, "
                             f"its totals row says {stated:,.0f}")
        if reconciled:
            stats["reconciled_with_totals_row"] = reconciled
    if totals_skipped and len(stats.get("reconciled_with_totals_row", [])) < len(value_cols):
        flags.append(f"asset table: {totals_skipped} row(s) labelled 'total' were left out")
    if bad_numbers:
        flags.append(f"asset table: {len(bad_numbers)} value(s) are not numbers, left empty: " + "; ".join(bad_numbers[:5]))
    for message, lines in bad_values.items():
        flags.append(f"asset table: {message} in {len(lines)} row(s) (e.g. line {lines[0]}): left empty")
    stats["rows"] = len(rows)
    stats["pd_total"] = math.fsum(r.get("pd_tiv") or 0 for r in rows)
    stats["bi_total"] = math.fsum(r.get("bi_income") or 0 for r in rows)
    for key, stated_key in (("pd_total", "stated_total_pd"), ("bi_total", "stated_total_bi")):
        stated = spec.get(stated_key)
        if stated and not math.isclose(stats[key], stated["value"], rel_tol=1e-6, abs_tol=1.0):
            flags.append(f"asset table: values read add up to {stats[key]:,.0f}, the sources state "
                         f"{stated['as_written']} ({stated_key.replace('stated_total_', 'total ').upper()})")
    return rows, flags, stats


def _check_row(row: dict, lists: dict, bad: dict) -> None:
    """Blank out values the tool would not accept, recording why."""
    def reject(key, message):
        bad.setdefault(message, []).append(row["line"])
        row[key] = None
        row.setdefault("rejected", set()).add(key)  # stays empty: no default may replace it

    def pick(key, options, label):
        if row.get(key) is None:
            return
        match = {_norm(o): o for o in options}.get(_norm(row[key]))
        if match is None:
            reject(key, f"{label} '{row[key]}' not in the tool's list{_closest(row[key], options)}")
        else:
            row[key] = match

    pick("sector", lists["Sector"], "sector")
    if row.get("occupancy") is not None:
        allowed = lists["occupancy_by_sector"].get(row.get("sector") or "", [])
        pick("occupancy", allowed or lists["Occupancy"], f"occupancy for sector {row.get('sector')}")
    pick("country", lists["RiskCountry"], "country")
    if row.get("cat_location") is not None:
        pick("cat_location", lists["cat_locations_by_country"].get(row.get("country") or "", []) + CUSTOM_LOCATIONS,
             f"CAT location for {row.get('country')}")
    pick("construction_type", lists["ConstructionType"], "construction type")
    pick("sprinkler", lists["Sprinkler"], "sprinkler")
    for key, options in (("bi_mip_months", lists["BIMIP"]), ("bi_waiting_period_days", lists["WaitingPeriod"])):
        if row.get(key) is not None and row[key] not in [float(o) for o in options]:
            reject(key, f"{key} {row[key]:g} not in the tool's list")


# ---------------------------------------------------------------- filling the tool

def fill(extracted: dict, job_dir: Path, contract: dict, today: date) -> tuple[Filler, dict]:
    f = Filler(contract)
    lists = contract["lists"]
    policy = extracted.get("policy") or {}
    ccy = f.listed("quoting currency", policy.get("quoting_currency"), lists["CCY"])
    if ccy is None:
        f.flags.append("quoting currency not stated: the tool's default is kept, amounts may be in the wrong currency")

    f.put(POLICY, "Reinsurance_Type", policy.get("reinsurance_type"))
    f.put(POLICY, "New_Renewal", policy.get("new_renewal"))
    f.put(POLICY, "Quote_Date", excel.excel_date(today))
    for key, cell in (("inception_date", "Policy_Inception_Date"), ("expiry_date", "Policy_Expiry_Date")):
        if policy.get(key):
            f.put(POLICY, cell, excel.excel_date(policy[key]))
    if policy.get("inception_date") and policy.get("expiry_date") and policy["expiry_date"] <= policy["inception_date"]:
        f.flags.append("expiry date is not after the inception date")
    f.put(POLICY, "Quoting_Currency", ccy)
    f.put(SUMMARY, "Display_Currency", ccy)
    for key, cell in POLICY_TEXT.items():
        f.put(POLICY, cell, policy.get(key))
    f.listed("broker", policy.get("broker"), lists["Brokers"], strict=False)

    for key, cell in COVERAGE.items():
        f.put(POLICY, cell, (extracted.get("coverage") or {}).get(key))
    bi = extracted.get("bi") or {}
    f.put(POLICY, "BI_MIP", f.listed("BI MIP (months)", bi.get("mip_months"), lists["BIMIP"]))
    f.put(POLICY, "BI_Waiting_Period", f.listed("BI waiting period (days)", bi.get("waiting_period_days"), lists["WaitingPeriod"]))
    f.put(POLICY, "CBI_MIP", bi.get("cbi_mip_months"))
    f.put(POLICY, "CBI_Waiting_Period", bi.get("cbi_waiting_period_days"))
    for key, cell in DEDUCTIONS.items():
        pct = (extracted.get("deductions") or {}).get(key)
        f.put(POLICY, cell, pct["percent"] / 100 if pct else None)

    def money(amount, what):
        if not amount:
            return None
        if ccy and amount.get("currency") and amount["currency"] != ccy:
            f.flags.append(f"{what} is in {amount['currency']}, the quote in {ccy}: left empty")
            return None
        return amount["value"]

    layers, seen = extracted.get("layers") or [], set()
    if not layers:
        f.flags.append("no layer stated: limits, excess and premium left empty")
    separate_bi = any(layer.get("bi_limit") for layer in layers)
    f.put(POLICY, "Separate_PD_and_BI_Limits_Primary", "Yes" if separate_bi else "No")
    for layer in layers:
        name = layer["layer"]
        if name in seen:
            f.flags.append(f"{name} appears twice: only the first is used")
            continue
        seen.add(name)
        s = LAYERS[name]
        f.put(POLICY, f"Offered_GGWP_{s}", money(layer.get("offered_premium"), f"{name} premium"))
        if layer.get("our_share"):
            f.put(SUMMARY, f"BRE_Share_{s}", layer["our_share"]["percent"] / 100)
        else:
            f.flags.append(f"{name}: our share not stated")
        main = {k: money(layer.get(k), f"{name} {k}") for k in ("limit", "excess", "deductible")}
        if main["limit"] is None:
            f.flags.append(f"{name}: no limit stated")
        f.put(POLICY, f"Limit_PD_{s}", main["limit"])
        f.put(POLICY, f"Excess_PD_{s}", main["excess"] if main["excess"] is not None else (0 if name == "primary" else None))
        f.put(POLICY, f"Deductible_PD_{s}", main["deductible"])
        if separate_bi:
            for k, cell in (("bi_limit", "Limit_BI"), ("bi_excess", "Excess_BI"), ("bi_deductible", "Deductible_BI")):
                f.put(POLICY, f"{cell}_{s}", money(layer.get(k), f"{name} {k}"))
        defaulted = []
        for peril, label in (("eq", "EQ"), ("ws", "WS"), ("fl", "FL")):
            p = layer.get(peril)
            if p and p.get("excluded"):
                f.put(POLICY, f"Limit_{label}_{s}", 0)
                f.info.append(f"{name}: {label} excluded (limit 0)")
                continue
            for k, cell in (("limit", "Limit"), ("excess", "Excess"), ("deductible", "Deductible")):
                value = money((p or {}).get(k), f"{name} {label} {k}")
                if value is None and main[k] is not None:
                    value = main[k]
                    if label not in defaulted:
                        defaulted.append(label)
                f.put(POLICY, f"{cell}_{label}_{s}", value)
        if defaulted:
            f.info.append(f"{name}: no separate {'/'.join(defaulted)} terms stated, the layer's own limit/excess/deductible used")

    risk = extracted.get("risk") or {}
    sector = f.listed("sector", risk.get("sector"), lists["Sector"])
    f.put(RISK, "Sector", sector)
    occupancy = None
    if risk.get("occupancy"):
        occupancy = f.listed(f"occupancy for sector {sector}", risk["occupancy"],
                             lists["occupancy_by_sector"].get(sector or "", lists["Occupancy"]))
        f.put(RISK, "Occupancy", occupancy)
    f.put(RISK, "Main_Risk_Country", f.listed("main risk country", risk.get("main_risk_country"), lists["RiskCountry"]))

    stats = {"assets": [], "losses": 0}
    all_rows = []
    for spec in extracted.get("asset_tables") or []:
        rows, flags, st = read_asset_table(spec, job_dir, lists, ccy)
        f.flags.extend(flags)
        stats["assets"].append(st)
        all_rows.extend(rows)
    if not extracted.get("asset_tables"):
        f.flags.append("no schedule of values found: asset table left empty")
    if len(all_rows) > MAX_ROWS["assets"]:
        f.flags.append(f"{len(all_rows)} assets, the tool takes {MAX_ROWS['assets']}: the rest were left out")
        all_rows = all_rows[:MAX_ROWS["assets"]]
    main = {"sector": sector, "occupancy": occupancy, "country": f.writes.get(f"{RISK}.Main_Risk_Country", (0, 0, None))[2]}
    for key, value in main.items():  # assets without their own value take the main risk's
        rows = [r for r in all_rows if r.get(key) is None and key not in r.get("rejected", ())
                and (key != "occupancy" or r.get("sector") in (None, sector))]
        if value is not None and rows:
            for r in rows:
                r[key] = value
                if key == "sector" and r.get("occupancy") is not None and r["occupancy"] not in lists["occupancy_by_sector"].get(value, []):
                    r["occupancy"] = None
            f.info.append(f"{len(rows)} asset(s) without a {key} given the main risk's: {value}")
    for key, label in (("sector", "sector"), ("occupancy", "occupancy"), ("country", "country"), ("pd_tiv", "PD value")):
        missing = sum(1 for r in all_rows if r.get(key) is None)
        if missing:
            f.flags.append(f"{missing} of {len(all_rows)} assets have no {label}: the tool cannot rate them until it is filled in")
    _fill_assets(f, all_rows, bi)
    stats["asset_rows"] = len(all_rows)
    stats["pd_written"] = math.fsum(r.get("pd_tiv") or 0 for r in all_rows)
    stats["bi_written"] = math.fsum(r.get("bi_income") or 0 for r in all_rows)

    history = extracted.get("loss_history") or {}
    losses = [x for x in history.get("losses") or [] if money(x.get("amount"), f"loss of {x.get('date_of_loss')}") is not None]
    losses = losses[:MAX_ROWS["losses"]]
    t = contract["tables"][EXPERIENCE]
    start = t["start_row"]
    cols = {k: v["column"] for k, v in t["columns"].items()}
    f.column(EXPERIENCE, cols["DOL"], start, [excel.excel_date(x["date_of_loss"]) if x.get("date_of_loss") else None for x in losses])
    f.column(EXPERIENCE, cols["Ground_Up_Loss_Total"], start, [x["amount"]["value"] for x in losses])
    f.column(EXPERIENCE, cols["Non_CAT_CAT"], start, [x.get("cat") for x in losses])
    if history.get("years"):
        f.put(EXPERIENCE, "Number_of_Years", history["years"])
    elif losses:
        f.flags.append("loss history: number of years not stated, the tool's default is kept")
    stats["losses"] = len(losses)
    return f, stats


def _fill_assets(f: Filler, rows: list[dict], bi: dict) -> None:
    """One tool row per asset; the tool numbers them itself (column A is a formula)."""
    t = f.c["tables"][ASSETS]
    start = t["start_row"]
    col = {k: t["columns"][v]["column"] for k, v in ASSET_COLUMNS.items()}
    if not rows:
        return
    policy_defaults = {"bi_mip_months": bi.get("mip_months"), "bi_waiting_period_days": bi.get("waiting_period_days")}
    used_default = set()
    for key in ASSET_COLUMNS:
        values = []
        for r in rows:
            v = r.get(key)
            if v is None and key in policy_defaults and (r.get("bi_income") or 0) > 0 and policy_defaults[key] is not None:
                v = policy_defaults[key]
                used_default.add(key)
            if isinstance(v, str):
                v = excel.as_text(v)
            values.append(v)
        f.column(ASSETS, col[key], start, values)
    for key in sorted(used_default):
        f.info.append(f"assets: the policy's {key.replace('_', ' ')} used where the schedule gives none")


# ---------------------------------------------------------------- running the tool and checking it

def _same(written, read) -> bool:
    if isinstance(written, str):
        return _norm(written.lstrip("'")) == _norm(read.get("text") if read.get("value") is None else read["value"])
    value = read.get("value")
    return isinstance(value, (int, float)) and math.isclose(float(written), float(value), rel_tol=1e-9, abs_tol=1e-6)


def run(extracted: dict, out_dir: Path, today: date | None = None, excel_run=excel.run) -> dict:
    job_dir = out_dir.parent.parent  # out_dir is <job>/output/property_pricing
    contract = load_contract()
    template = TOOL_DIR / contract["template"]
    if excel_run is excel.run and not template.is_file():
        raise RuntimeError(f"the tool template is missing: copy the blank '{contract['template']}' into {TOOL_DIR} "
                           "(it is not in git; see tool/README.md), then retry this job")
    flags = check_evidence(extracted, job_dir)
    f, stats = fill(extracted, job_dir, contract, today or date.today())
    flags += f.flags

    insured = (extracted.get("policy") or {}).get("original_insured") or "Pricing request"
    workbook = out_dir / f"{safe_name(insured, 40)} - Pricing v{contract['version']}.xlsb"
    job = excel.ExcelJob(template=template, output=workbook, activate=POLICY)
    for sheet, cell, value in f.writes.values():
        job.write(sheet, cell, value)
    job.tables = f.tables
    reads = {key: (sheet, cell) for key, (sheet, cell, _) in f.writes.items()}  # read back what was written
    for key, fd in contract["fields"].items():
        if fd["direction"] == "OUT":
            reads[key] = (fd["sheet"], fd["cell"])
        if fd.get("label_cell") and key in f.writes:  # the layout check: labels next to the cells written
            reads[f"label:{key}"] = (fd["sheet"], fd["label_cell"])
    vc = contract["version_check"]
    reads["version"] = (vc["sheet"], vc["cell"])
    for sheet, cell in reads.values():
        job.read(sheet, cell)
    n = stats["asset_rows"]
    at = contract["tables"][ASSETS]
    job.read_ranges.append({"sheet": ASSETS, "range": f"A{at['header_row']}:W{at['header_row']}"})
    if n:
        job.read_ranges.append({"sheet": ASSETS, "range": f"A{at['start_row']}:W{at['start_row'] + n - 1}"})
    insureds = contract["runtime_lists"]["orig_insured"]  # client names: read from the workbook, never kept in git
    job.read_ranges.append({"sheet": insureds["sheet"], "range": insureds["range"]})

    result = excel_run(job, out_dir / "_excel")
    got = result["reads"]
    value = lambda key: got[f"{reads[key][0]}!{reads[key][1]}"]  # noqa: E731

    if _norm(vc["text"]) not in _norm(value("version")["text"]):
        raise RuntimeError(f"the template is not {vc['text']} (found '{value('version')['text']}'): update tool/contract.json")
    drift = [f"{key} (expected '{contract['fields'][key]['label']}', found '{value('label:' + key)['text']}')"
             for key in f.writes if f"label:{key}" in reads
             and _norm(value("label:" + key)["text"]) != _norm(contract["fields"][key]["label"])]
    header = result["ranges"].get(f"{ASSETS}!A{at['header_row']}:W{at['header_row']}", [[]])[0]
    for name, cdef in at["columns"].items():
        idx = ord(cdef["column"]) - 65
        if cdef.get("header") and (idx >= len(header) or _norm(header[idx]) != _norm(cdef["header"])):
            drift.append(f"asset column {cdef['column']} (expected '{cdef['header']}')")
    if drift:
        raise RuntimeError("the workbook's layout differs from tool/contract.json, nothing can be trusted: " + "; ".join(drift[:5]))

    not_taken = [key.split(".", 1)[1] for key, (_, _, v) in f.writes.items() if not _same(v, value(key))]
    if not_taken:
        flags.append("the tool did not keep these inputs as written: " + ", ".join(not_taken[:10]))

    outputs = {key.split(".", 1)[1]: value(key)["value"] if value(key)["value"] is not None else value(key)["text"]
               for key, fd in contract["fields"].items() if fd["direction"] == "OUT"}
    errors = [k for k, v in outputs.items() if isinstance(v, str) and v.startswith("#")]
    if n:
        rows = result["ranges"][f"{ASSETS}!A{at['start_row']}:W{at['start_row'] + n - 1}"]
        bad = [i + at["start_row"] for i, r in enumerate(rows) if any(v == "#ERROR" for v in r)]
        if bad:
            flags.append(f"asset table: Excel errors in {len(bad)} row(s), e.g. row {bad[0]}")
        written_pd = math.fsum(float(r[6]) for r in rows if isinstance(r[6], (int, float)))
        if not math.isclose(written_pd, stats["pd_written"], rel_tol=1e-9, abs_tol=0.5):
            flags.append(f"asset table: PD values in the workbook add up to {written_pd:,.0f}, "
                         f"{stats['pd_written']:,.0f} were written")
        tool_count = outputs.get("Number_of_Assets")
        if isinstance(tool_count, (int, float)) and int(tool_count) != n:
            flags.append(f"the tool counts {int(tool_count)} assets, {n} were written")
        tiv_pd = outputs.get("TIV_PD")
        if isinstance(tiv_pd, (int, float)) and not math.isclose(tiv_pd, stats["pd_written"], rel_tol=1e-6, abs_tol=1.0):
            flags.append(f"the tool's total PD value ({tiv_pd:,.0f}) differs from the PD values written "
                         f"({stats['pd_written']:,.0f})")

    known = [r[0] for r in result["ranges"].get(f"{insureds['sheet']}!{insureds['range']}", [])
             if r and isinstance(r[0], str) and r[0].strip()]
    insured_info = []
    if known and (extracted.get("policy") or {}).get("original_insured"):
        probe = Filler(contract)
        probe.listed("insured", extracted["policy"]["original_insured"], known, strict=False)
        insured_info = probe.info
    f.info[:0] = insured_info

    ccy = (extracted.get("policy") or {}).get("quoting_currency")
    facts = _facts(extracted, outputs, stats, ccy, workbook.name, contract["version"])
    if f.info:
        facts["Notes"] = "; ".join(f.info[:4])
    if errors:
        flags.append("the tool shows errors in: " + ", ".join(errors[:8]))
    if not isinstance(outputs.get("Technical_Premium_UW_Total"), (int, float)):
        flags.append("the tool produced no technical premium: complete the missing inputs in the workbook")

    computed = {
        "tool": f"{contract['tool']} v{contract['version']}", "workbook": workbook.name,
        "inputs_written": {k: v for k, (_, _, v) in f.writes.items()},
        "asset_tables": stats["assets"], "asset_rows": n, "losses": stats["losses"],
        "outputs": outputs, "checks": flags, "notes": f.info,
        "excel": {"version": result.get("excel_version"), "seconds": result.get("seconds")},
    }
    (out_dir / "computed.json").write_text(json.dumps(computed, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    digest = hashlib.sha256(template.read_bytes()).hexdigest()[:12] if template.is_file() else "missing"
    return {"facts": facts, "flags": flags,
            "versions": {"Property Pricing Tool": f"{contract['version']} (template {digest})",
                         "Excel": str(result.get("excel_version"))}}


def _facts(extracted, outputs, stats, ccy, workbook, version) -> dict:
    facts = {}
    policy = extracted.get("policy") or {}
    if policy.get("original_insured"):
        facts["Insured"] = policy["original_insured"]
    parts = []
    for layer in extracted.get("layers") or []:
        lim, xs = layer.get("limit"), layer.get("excess")
        text = format_amount(lim["value"], lim["currency"]) if lim else "limit ?"
        if xs and xs["value"]:
            text += f" xs {format_amount(xs['value'])}"
        if layer.get("our_share"):
            text += f" ({layer['our_share']['percent']:g}%)"
        parts.append(f"{LAYERS[layer['layer']]}: {text}")
    if parts:
        facts["Layers"] = "; ".join(parts)
    if stats["asset_rows"]:
        facts["Assets"] = f"{stats['asset_rows']:,} (PD {format_amount(stats['pd_written'], ccy)}, BI {format_amount(stats['bi_written'], ccy)})"
    for key, label in (("GGWP_Net_of_NCD_Total", "Offered GGWP (tool)"), ("Technical_Premium_UW_Total", "Technical premium UW (tool)")):
        v = outputs.get(key)
        if isinstance(v, (int, float)):
            facts[label] = format_amount(v, ccy)
    adequacy = []
    for layer in extracted.get("layers") or []:
        v = outputs.get(f"Technical_Premium_Adequacy_UW_{LAYERS[layer['layer']]}")
        if isinstance(v, (int, float)):
            adequacy.append(f"{LAYERS[layer['layer']]} {v:.0%}")
    if adequacy:
        facts["Adequacy UW (tool)"] = ", ".join(adequacy)
    facts["Workbook"] = f"{workbook} (Property Pricing Tool v{version})"
    return facts
