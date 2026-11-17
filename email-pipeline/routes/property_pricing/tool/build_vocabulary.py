"""Regenerate vocabulary.md (the tool's dropdown values, read by the agent) from contract.json.

    uv run python routes/property_pricing/tool/build_vocabulary.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def main() -> None:
    c = json.loads((HERE / "contract.json").read_text(encoding="utf-8"))
    lists = c["lists"]
    out = [
        f"# Property Pricing Tool v{c['version']}: allowed values",
        "",
        "Use these values exactly as written (case, spaces and punctuation included).",
        "Values not in these lists are rejected by the pipeline.",
        "",
        "## Sectors and their occupancies",
        "",
        "Each occupancy belongs to exactly one sector.",
        "",
    ]
    for sector, occs in lists["occupancy_by_sector"].items():
        out.append(f"- **{sector}**: " + "; ".join(occs))
    out += ["", "## Countries", "", "; ".join(lists["RiskCountry"]), ""]
    multi = {k: v for k, v in lists["cat_locations_by_country"].items() if len(v) > 1}
    out += ["## CAT locations (only for these countries; elsewhere the country is the location)", ""]
    out += [f"- **{k}**: " + "; ".join(v) for k, v in multi.items()]
    out += ["", "## Other lists", ""]
    for key, title in [("ConstructionType", "Construction type"), ("Sprinkler", "Sprinkler"), ("BIMIP", "BI MIP (months)"),
                       ("WaitingPeriod", "BI waiting period (days)"), ("CCY", "Currencies"), ("Brokers", "Brokers (known)")]:
        out.append(f"- **{title}**: " + "; ".join(map(str, lists[key])))
    (HERE / "vocabulary.md").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
