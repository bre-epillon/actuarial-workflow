import json
import os

import pytest

from conftest import ROUTES_DIR
from pipeline.classifier import parse_classification
from pipeline.config import load_config
from pipeline.jobs import content_hash, create_job, find_job, load_jobs, safe_name, unique_path
from pipeline.notify import build_payload
from pipeline.routes import load_postprocess, load_routes, parse_route_file


def test_repo_routes_are_valid():
    routes = load_routes(ROUTES_DIR)
    assert {"large_loss", "smoke_test"} <= set(routes)
    assert "Bash" not in routes["large_loss"].allowed_tools
    assert routes["large_loss"].output_schema is not None
    assert callable(load_postprocess(routes["large_loss"]))
    for f in ROUTES_DIR.glob("*/ROUTE.md"):
        parse_route_file(f)  # raises on any invalid route


def _route(tmp_path, frontmatter: str, name: str = "foo"):
    d = tmp_path / name
    d.mkdir()
    (d / "ROUTE.md").write_text(f"---\n{frontmatter}\n---\nbody", encoding="utf-8")
    return d / "ROUTE.md"


def test_route_name_must_match_folder(tmp_path):
    with pytest.raises(ValueError):
        parse_route_file(_route(tmp_path, "name: bar\ndescription: x"))


@pytest.mark.parametrize("tools", ["[Read, WebFetch]", "[Read, Raed]"])
def test_route_rejects_web_and_unknown_tools(tmp_path, tools):
    with pytest.raises(ValueError):
        parse_route_file(_route(tmp_path, f"name: foo\ndescription: x\nallowed_tools: {tools}"))


def test_route_schema_and_postprocess_are_checked(tmp_path):
    path = _route(tmp_path, "name: foo\ndescription: x\nallowed_tools: [Read, Write]\noutput_schema: s.json\npostprocess: p.py")
    (path.parent / "s.json").write_text('{"type": "object"}', encoding="utf-8")
    with pytest.raises(ValueError, match="p.py"):
        parse_route_file(path)
    (path.parent / "p.py").write_text("def run(extracted, out_dir):\n    return {}\n", encoding="utf-8")
    route = parse_route_file(path)
    assert route.output_schema == {"type": "object"} and route.postprocess.name == "p.py"
    (path.parent / "s.json").write_text('{"type": 42}', encoding="utf-8")
    with pytest.raises(Exception):
        parse_route_file(path)


def test_safe_name_windows():
    assert safe_name('RE: Loss? <A/B> "x"') == "RE_ Loss_ _A_B_ _x_"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("Loss advice - 100% QS") == "Loss advice - 100_ QS"  # the guard refuses '%' in paths
    long = safe_name("Large loss advice " * 10 + ".xlsx", 40)
    assert len(long) <= 40 and long.endswith(".xlsx")


def test_unique_path(tmp_path):
    (tmp_path / "a.csv").write_text("x")
    assert unique_path(tmp_path, "a.csv").name == "a_2.csv"
    assert unique_path(tmp_path, "b.csv").name == "b.csv"


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
    assert find_job(jobs_dir, job.id[-8:]).id == job.id
    with pytest.raises(LookupError):
        find_job(jobs_dir, "nope")


def test_failed_copy_leaves_no_half_job(tmp_path, monkeypatch):
    src = tmp_path / "mail.msg"
    src.write_bytes(b"x")
    jobs_dir = tmp_path / "jobs"

    def locked(*args, **kwargs):
        raise PermissionError("file is open in Outlook")

    monkeypatch.setattr("pipeline.jobs.shutil.copy2", locked)
    with pytest.raises(PermissionError):
        create_job(jobs_dir, src, content_hash(src))
    assert list(jobs_dir.iterdir()) == []
    monkeypatch.undo()
    assert create_job(jobs_dir, src, content_hash(src)).state == "new"  # next poll works


def test_manifest_save_waits_for_a_reader(tmp_path, monkeypatch):
    src = tmp_path / "mail.msg"
    src.write_bytes(b"x")
    job = create_job(tmp_path / "jobs", src, content_hash(src))
    real_replace, attempts = os.replace, []

    def busy_twice(a, b):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError("manifest.json is open in another process")
        real_replace(a, b)

    monkeypatch.setattr("pipeline.jobs.os.replace", busy_twice)
    monkeypatch.setattr("pipeline.jobs.time.sleep", lambda s: None)
    job.set_state("preprocessed")
    assert len(attempts) == 3 and load_jobs(tmp_path / "jobs")[0].state == "preprocessed"


def test_config_paths_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("EP_TEST_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    (tmp_path / "config.toml").write_text(
        'inbox_dir = "%EP_TEST_ROOT%/inbox"\njobs_dir = "$EP_TEST_ROOT/jobs"\nroutes_dir = "routes"\n', encoding="utf-8")
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.jobs_dir == (tmp_path / "data" / "jobs").resolve()
    assert cfg.routes_dir == (tmp_path / "routes").resolve()
    if cfg.inbox_dir != (tmp_path / "data" / "inbox").resolve():
        pytest.skip("%VAR% expansion is Windows-only")


def test_config_missing_gives_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.toml"):
        load_config(tmp_path / "config.toml")


def test_parse_classification_filters_unknown_routes():
    text = 'Sure: {"routes": [{"name": "large_loss", "confidence": 0.9, "reason": "r"}, {"name": "made_up", "confidence": 1}]}'
    out = parse_classification(text, {"large_loss"})
    assert [o["name"] for o in out] == ["large_loss"]
    structured = {"routes": [{"name": "a", "confidence": 0.2, "reason": ""}, {"name": "b", "confidence": 0.8, "reason": ""}]}
    assert [o["name"] for o in parse_classification(structured, {"a", "b"})] == ["b", "a"]


def test_parse_classification_rejects_non_json():
    with pytest.raises(ValueError):
        parse_classification("I am not sure", {"large_loss"})


def test_teams_payload_is_truncated():
    payload = build_payload("t", "x" * 10000, {"Job": "j", "Empty": ""})
    card = payload["attachments"][0]["content"]
    assert len(card["body"][2]["text"]) < 4200
    assert [f["title"] for f in card["body"][1]["facts"]] == ["Job"]
    json.dumps(payload)
