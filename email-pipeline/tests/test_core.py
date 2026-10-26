from pathlib import Path

import pytest

from pipeline.classifier import parse_classification
from pipeline.jobs import content_hash, create_job, load_jobs, safe_name
from pipeline.routes import load_routes, parse_route_file

ROUTES_DIR = Path(__file__).parents[1] / "routes"


def test_repo_routes_are_valid():
    routes = load_routes(ROUTES_DIR)
    assert {"large_loss", "smoke_test"} <= set(routes)
    assert "Bash" not in routes["large_loss"].allowed_tools


def test_route_name_must_match_folder(tmp_path):
    d = tmp_path / "foo"
    d.mkdir()
    (d / "ROUTE.md").write_text("---\nname: bar\ndescription: x\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_route_file(d / "ROUTE.md")


def test_safe_name_windows():
    assert safe_name('RE: Loss? <A/B> "x"') == "RE_ Loss_ _A_B_ _x_"


def test_hash_ignores_file_name_but_not_content(tmp_path):
    a, b, c = tmp_path / "a.msg", tmp_path / "b.msg", tmp_path / "c.msg"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    c.write_bytes(b"different")
    assert content_hash(a) == content_hash(b) != content_hash(c)


def test_create_and_reload_job(tmp_path):
    src = tmp_path / "mail.msg"
    src.write_bytes(b"x")
    jobs_dir = tmp_path / "jobs"
    job = create_job(jobs_dir, src, content_hash(src))
    job.set_state("preprocessed")
    [reloaded] = load_jobs(jobs_dir)
    assert reloaded.state == "preprocessed"
    assert (reloaded.original_dir / "mail.msg").exists()


def test_parse_classification_filters_unknown_routes():
    text = 'Sure: {"routes": [{"name": "large_loss", "confidence": 0.9, "reason": "r"}, {"name": "made_up", "confidence": 1}]}'
    out = parse_classification(text, {"large_loss"})
    assert [o["name"] for o in out] == ["large_loss"]


def test_parse_classification_rejects_non_json():
    with pytest.raises(ValueError):
        parse_classification("I am not sure", {"large_loss"})
