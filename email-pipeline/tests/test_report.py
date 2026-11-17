"""The status page: job verdicts, cases linked across stages, safe embedding of email text."""
import json

import pytest

from conftest import FIXTURES, ROUTES_DIR, first_body, job_from, valid_large_loss
from pipeline.preprocess import preprocess
from pipeline.report import build_report, write_report
from pipeline.routes import load_routes, parse_route_file


def done_job(jobs_dir, fixture, extracted=None, flags=(), subject=None):
    """A finished large_loss job as the pipeline would leave it."""
    job = job_from(jobs_dir, FIXTURES / fixture)
    preprocess(job)
    if extracted is not None:
        out = job.output_dir("large_loss")
        out.mkdir(parents=True)
        (out / "extracted.json").write_text(json.dumps(extracted(first_body(job.dir))), encoding="utf-8")
        (out / "summary.md").write_text("summary", encoding="utf-8")
        job.data["route_runs"]["large_loss"] = {"ok": True, "flags": list(flags), "facts": {}, "problems": []}
        job.data["classification"] = {"candidates": [], "accepted": ["large_loss"]}
    job.data["subject"] = subject or job.data["source_name"]
    job.set_state("done" if extracted is not None else "needs_review")
    return job


def update(previous, gross, **changes):
    def make(source):
        ex = valid_large_loss(source)
        ex["previous_estimate"] = dict(ex["previous_estimate"], value=previous)
        ex["gross_loss"] = dict(ex["gross_loss"], value=gross)
        return dict(ex, **changes)
    return make


@pytest.fixture
def routes():
    return load_routes(ROUTES_DIR)


def test_stages_of_one_loss_are_linked_and_cross_checked(tmp_path, routes):
    jobs = tmp_path / "jobs"
    done_job(jobs, "large_loss_advice.eml", update(10_000_000, 12_500_000))             # 21 Sep
    done_job(jobs, "loss_update_forwarded.eml", update(12_000_000, 15_000_000))         # 5 Oct: says 12.0m
    done_job(jobs, "cash_call.eml", lambda s: dict(valid_large_loss(s), notification_type="cash_call",
                                                     gross_loss=None, previous_estimate=None))
    done_job(jobs, "quarterly_accounts.eml")                                             # unrelated
    report = build_report(jobs, routes)

    [case] = report["cases"]
    assert [s["stage"] for s in case["steps"]] == ["update", "update", "cash_call"]  # ordered by email date
    assert case["keys"] == {"treaty_reference": "NWM-PCAT-2026-L2", "date_of_loss": "2026-09-14"}
    assert "first_advice" in case["stages"] and None not in case["stages"]
    [note] = case["notes"]
    assert "EUR 12,000,000" in note and "EUR 12,500,000" in note  # update contradicts the advice
    assert case["verdict"] == "check"
    assert any("not among the processed emails" in i for i in case["info"])  # the advice's own 10m
    assert report["counts"] == {"ok": 3, "review": 1}


def test_other_losses_and_incomplete_keys_stay_apart(tmp_path, routes):
    jobs = tmp_path / "jobs"
    done_job(jobs, "large_loss_advice.eml", update(10_000_000, 12_500_000))
    done_job(jobs, "loss_update_forwarded.eml", update(10_000_000, 12_500_000, treaty_reference="OTHER-TREATY"))
    done_job(jobs, "cash_call.eml", lambda s: dict(valid_large_loss(s), date_of_loss=None))
    cases = build_report(jobs, routes)["cases"]
    assert sorted(len(c["steps"]) for c in cases) == [1, 1]


def test_flags_make_a_job_need_a_look(tmp_path, routes):
    jobs = tmp_path / "jobs"
    done_job(jobs, "large_loss_advice.eml", update(10_000_000, 12_500_000), flags=["gross_loss: mismatch"])
    [job] = build_report(jobs, routes)["jobs"]
    assert job["verdict"] == "check" and job["routes"]["large_loss"]["summary"] == "summary"


def test_page_embeds_email_text_safely(tmp_path, routes):
    jobs = tmp_path / "jobs"
    done_job(jobs, "smoke_test.eml", subject='</script><script>alert("x")</script>')
    html = write_report(jobs, routes).read_text(encoding="utf-8")
    assert html.count("</script>") == 1  # only the page's own: email text cannot close it and inject code
    data = html.split("const DATA = ", 1)[1].split(";\nconst VERDICT", 1)[0]
    assert json.loads(data.replace("<\\/", "</"))["jobs"][0]["subject"].startswith("</script>")


def test_case_keys_must_exist_in_the_schema(tmp_path):
    route = tmp_path / "foo"
    route.mkdir()
    (route / "s.json").write_text('{"type": "object", "properties": {"a": {}}}', encoding="utf-8")
    (route / "ROUTE.md").write_text("---\nname: foo\ndescription: x\nallowed_tools: [Read, Write]\n"
                                    "output_schema: s.json\ncase_keys: [a, b]\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="b"):
        parse_route_file(route / "ROUTE.md")
