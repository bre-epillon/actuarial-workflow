import json

import pytest

from conftest import ROUTES_DIR, loss_job, valid_large_loss
from pipeline.amounts import amount_readings, check_amount, check_percent, currency_in
from pipeline.evidence import quote_problem
from pipeline.routes import load_postprocess, load_routes
from pipeline.runner import validate_output


@pytest.mark.parametrize("text,expected", [
    ("EUR 12.5m", [12_500_000]),
    ("EUR 10,000,000", [10_000_000]),
    ("12.500.000,00 €", [12_500_000]),
    ("EUR 1 250 000", [1_250_000]),
    ("USD 1.2bn", [1_200_000_000]),
    ("CHF 1'250'000.50", [1_250_000.5]),
    ("-2.5 Mio", [-2_500_000]),
    ("EUR 15.0 million", [15_000_000]),
    ("EUR 375,000", [375_000]),      # a lone separator before 3 digits groups thousands...
    ("EUR 1.234m", [1_234_000]),     # ...except before a multiplier
    ("0,125", [0.125]),
    ("EUR 12.5m (100%)", [12_500_000]),  # percentages are not amounts
])
def test_amount_readings(text, expected):
    assert amount_readings(text) == pytest.approx(expected)


def test_check_amount():
    assert check_amount(12_500_000, "EUR 12.5m", "EUR") is None
    assert "does not match" in check_amount(1_250_000, "EUR 12.5m", "EUR")
    assert "does not match" in check_amount(1.234e9, "EUR 1.234m", "EUR")  # x1000 slip
    assert "only the amount" in check_amount(12_500_000, "as at 30/09/2026: EUR 12.5m", "EUR")
    assert "only the amount" in check_amount(10e6, "EUR 12.5m (previous estimate EUR 10m)", "EUR")
    assert "currency" in check_amount(12_500_000, "USD 12.5m", "EUR")
    assert "no number" in check_amount(1, "twelve million", None)
    assert currency_in("12.5m €") == "EUR" and currency_in("USD 5m") == "USD" and currency_in("$5m") is None


def test_check_percent():
    assert check_percent(7.5, "Your share: 7.5%") is None
    assert check_percent(7.5, "7,50 %") is None
    assert check_percent(0.075, "7.5%") is not None
    assert check_percent(100, "Your 7.5% share of the 100% loss") is not None
    assert check_percent(7.5, "seven and a half") is not None


def test_quote_must_be_in_the_cited_file(tmp_path):
    job = tmp_path / "job"
    (job / "extracted").mkdir(parents=True)
    (job / "extracted" / "body.txt").write_text("Gross loss:\n  EUR 12.5m\xa0(100%)", encoding="utf-8")
    assert quote_problem(job, "extracted/body.txt", "gross LOSS: EUR 12.5m (100%)") is None  # whitespace/case
    assert "not found" in quote_problem(job, "extracted/body.txt", "EUR 15.0m")
    assert "does not exist" in quote_problem(job, "extracted/missing.txt", "x")
    assert "outside" in quote_problem(job, "../secret.txt", "x")
    assert "network" in quote_problem(job, r"\\attacker.example\share\x", "x")


@pytest.fixture
def large_loss():
    return load_routes(ROUTES_DIR)["large_loss"]


def test_large_loss_schema_accepts_valid_and_rejects_invented_fields(tmp_path, large_loss):
    path = tmp_path / "extracted.json"
    path.write_text(json.dumps(valid_large_loss()), encoding="utf-8")
    assert validate_output(path, large_loss.output_schema) == []
    bad = valid_large_loss()
    bad["date_of_loss"] = "14/09/2026"
    bad["gross_loss"]["currency"] = "euro"
    bad["net_loss"] = 1
    path.write_text(json.dumps(bad), encoding="utf-8")
    errors = validate_output(path, large_loss.output_schema)
    assert len(errors) == 3 and any("date_of_loss" in e for e in errors)


def test_large_loss_postprocess(tmp_path, large_loss):
    run = load_postprocess(large_loss)
    out = loss_job(tmp_path)
    result = run(valid_large_loss(), out)
    assert result["flags"] == []
    assert result["facts"]["Movement vs previous"] == "EUR +2,500,000"
    assert json.loads((out / "computed.json").read_text())["movement_vs_previous"] == 2_500_000

    wrong = valid_large_loss()
    wrong["gross_loss"]["value"] = 1_250_000  # mis-scaled by the agent
    wrong["previous_estimate"]["as_written"] = "EUR 9,000,000"  # not what the email says
    wrong["previous_estimate"]["value"] = 9_000_000
    wrong["our_share"]["percent"] = 0.075
    wrong["sources"]["country"]["quote"] = "Country: Elsewhere"  # invented quote
    del wrong["sources"]["cedent"]
    wrong["treaty_reference"] = None
    flags = run(wrong, out)["flags"]
    assert any(f.startswith("gross_loss: value") for f in flags)
    assert any(f.startswith("previous_estimate:") and "not found" in f for f in flags)
    assert any(f.startswith("our_share") for f in flags)
    assert any(f.startswith("country:") and "not found" in f for f in flags)
    assert any("cedent" in f for f in flags) and any("treaty_reference" in f for f in flags)


def test_cash_call_needs_no_gross_loss(tmp_path, large_loss):
    out = loss_job(tmp_path)
    body = out.parent.parent / "extracted" / "x" / "body.txt"
    body.write_text(body.read_text(encoding="utf-8") + "\nCash call amount: EUR 375,000\nDue date 30 October 2026\n",
                    encoding="utf-8")
    cash_call = dict(valid_large_loss(), notification_type="cash_call", gross_loss=None, previous_estimate=None,
                     cash_call_due_date="2026-10-30",
                     cash_call={"value": 375000, "currency": "EUR", "as_written": "EUR 375,000", "source": "extracted/x/body.txt"})
    cash_call["sources"]["cash_call_due_date"] = {"file": "extracted/x/body.txt", "quote": "Due date 30 October 2026"}
    result = load_postprocess(large_loss)(cash_call, out)
    assert result["flags"] == []
    assert result["facts"]["Cash call"] == "EUR 375,000 (due 2026-10-30)"
