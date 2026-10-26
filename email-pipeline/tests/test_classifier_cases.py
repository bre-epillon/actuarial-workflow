"""Offline checks of the classifier regression cases (the live run is `email-pipeline eval-classifier`)."""
import yaml

from conftest import FIXTURES, REPO, ROUTES_DIR, job_from
from pipeline.preprocess import preprocess
from pipeline.routes import load_routes


def test_cases_cover_every_route_and_preprocess_cleanly(tmp_path):
    cases = yaml.safe_load((REPO / "tests" / "classifier_cases.yaml").read_text(encoding="utf-8"))
    routes = load_routes(ROUTES_DIR)
    covered = set()
    for i, case in enumerate(cases):
        src = FIXTURES / case["fixture"]
        expected = set(case.get("expected") or [])
        assert src.exists(), case
        assert expected <= set(routes), case
        covered |= expected
        summary = preprocess(job_from(tmp_path / str(i), src))
        assert summary["emails"] and not summary["warnings"], case
    assert covered == set(routes), "every route needs at least one regression case"
