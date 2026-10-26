import io
import json
import zipfile
from email.message import EmailMessage
from types import SimpleNamespace

from conftest import FIXTURES, job_from
from pipeline.preprocess import _Extractor, html_to_text, preprocess


def _by_name(records):
    return {r["name"]: r for r in records}


def test_eml_with_html_table_pdf_and_excel(tmp_path):
    job = job_from(tmp_path, FIXTURES / "large_loss_advice.eml")
    summary = preprocess(job)
    assert summary["warnings"] == []
    [mail] = summary["emails"]
    assert mail["subject"].startswith("Large Loss Advice")
    body = (job.dir / mail["body"]).read_text(encoding="utf-8")
    assert "EUR 12.5m" in body and body.startswith("From: Claims Desk")
    # the HTML version keeps table rows together
    assert "Current gross loss estimate (100%) | EUR 12.5m" in (job.dir / mail["body_html"]).read_text(encoding="utf-8")
    atts = _by_name(mail["attachments"])
    [pdf_txt] = atts["loss_advice.pdf"]["derived"]
    assert "EUR 12,500,000" in (job.dir / pdf_txt).read_text(encoding="utf-8")
    [csv] = atts["loss_details.xlsx"]["derived"]
    assert "Harbour Town" in (job.dir / csv).read_text(encoding="utf-8")
    assert json.loads((job.extracted_dir / "summary.json").read_text(encoding="utf-8")) == summary


def test_attached_email_is_expanded(tmp_path):
    job = job_from(tmp_path, FIXTURES / "loss_update_forwarded.eml")
    outer, inner = preprocess(job)["emails"]
    assert outer["subject"] == "FW: Loss update Storm Example"
    assert inner["embedded_in"] == outer["body"]
    assert outer["attachments"] == [{"name": inner["subject"], "email": inner["body"]}]
    assert "EUR 15.0m" in (job.dir / inner["body"]).read_text(encoding="utf-8")


def test_docx_text_keeps_tables(tmp_path):
    job = job_from(tmp_path, FIXTURES / "cash_call.eml")
    [mail] = preprocess(job)["emails"]
    [txt] = mail["attachments"][0]["derived"]
    assert "Cash call amount | EUR 375,000" in (job.dir / txt).read_text(encoding="utf-8")


def test_loose_files_zip_and_duplicates(tmp_path):
    src = tmp_path / "drop"
    src.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("reports/q3.csv", "a,b\n1,2\n")
        zf.writestr("../escape.txt", "zip-slip attempt")
    (src / "bundle.zip").write_bytes(buf.getvalue())
    (src / "notes.txt").write_text("hello", encoding="utf-8")
    job = job_from(tmp_path / "jobs", src)
    summary = preprocess(job)
    names = _by_name(summary["files"])
    assert {"bundle.zip", "notes.txt", "reports/q3.csv", "../escape.txt"} <= set(names)
    for r in summary["files"]:
        assert (job.dir / r["path"]).resolve().is_relative_to(job.extracted_dir.resolve())
    assert names["reports/q3.csv"]["from_zip"].endswith("bundle.zip")


def test_rerun_starts_clean(tmp_path):
    job = job_from(tmp_path, FIXTURES / "smoke_test.eml")
    first = preprocess(job)
    second = preprocess(job)
    assert first == second  # no "_2" copies from the first run


def test_msg_objects_with_embedded_email(tmp_path):
    """Outlook .msg via stand-in objects shaped like extract_msg's (no real .msg fixture needed)."""
    def att(name, data, cid=None):
        return SimpleNamespace(longFilename=name, shortFilename=None, data=data, cid=cid)

    inner = SimpleNamespace(sender="cedent@x.test", to="", cc="", date=None, subject="Loss update",
                            body="revised EUR 15.0m", htmlBody=None, attachments=[])
    outer = SimpleNamespace(
        sender="broker@x.test", to="us@x.test", cc=None, date="2026-10-05", subject="FW: Loss: update?",
        body=None, htmlBody=b"<html><body><p>See below</p><table><tr><td>A</td><td>1</td></tr></table></body></html>",
        attachments=[att("image001.png", b"\x89PNG", cid="img1"), att("a.csv", b"x,y\n"), att("a.csv", b"x,z\n"),
                     SimpleNamespace(longFilename=None, shortFilename=None, data=inner, cid=None)],
    )
    src = tmp_path / "x.msg"
    src.write_bytes(b"placeholder")
    job = job_from(tmp_path / "jobs", src)
    ex = _Extractor(job)
    record = ex.msg(outer, "original/x.msg", depth=0)
    body = (job.dir / record["body"]).read_text(encoding="utf-8")
    assert "Subject: FW: Loss: update?" in body and "A | 1" in body  # HTML-only body converted
    files = [a for a in record["attachments"] if "path" in a]
    assert [a.get("inline", False) for a in files] == [True, False, False]
    assert files[1]["path"] != files[2]["path"]  # same name, both kept
    assert record["attachments"][-1] == {"name": "Loss update", "email": ex.summary["emails"][1]["body"]}


def test_broken_attachment_does_not_lose_the_email(tmp_path):
    class Broken:
        longFilename = "corrupt.xlsx"

        @property
        def data(self):
            raise ValueError("bad OLE stream")

    mail = SimpleNamespace(sender="a@x.test", to="", cc="", date=None, subject="Loss advice", body="EUR 1m",
                           htmlBody=None, attachments=[Broken(), SimpleNamespace(longFilename="ok.txt", data=b"ok", cid=None)])
    src = tmp_path / "x.msg"
    src.write_bytes(b"placeholder")
    ex = _Extractor(job_from(tmp_path / "jobs", src))
    record = ex.msg(mail, "original/x.msg", depth=0)
    assert [a["name"] for a in record["attachments"]] == ["ok.txt"]
    assert "corrupt.xlsx" in ex.summary["warnings"][0]


def test_unreadable_msg_body_keeps_the_rest(tmp_path):
    class Mail:
        sender, to, cc, date, subject, htmlBody = "a@x.test", "", "", None, "Loss advice", None
        attachments = [SimpleNamespace(longFilename="ok.txt", data=b"ok", cid=None)]

        @property
        def body(self):
            raise ValueError("malformed RTF")

    src = tmp_path / "x.msg"
    src.write_bytes(b"placeholder")
    ex = _Extractor(job_from(tmp_path / "jobs", src))
    record = ex.msg(Mail(), "original/x.msg", depth=0)
    assert [a["name"] for a in record["attachments"]] == ["ok.txt"]
    assert any("body" in w for w in ex.summary["warnings"])


def test_eml_edge_cases_lose_nothing(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "split body"
    msg.set_content("first part")
    msg.add_attachment(b"%PDF-1.4 not really", maintype="application", subtype="pdf", filename="a.pdf")
    msg.add_attachment("second part, after the attachment", disposition="inline")  # Apple Mail style
    raw = msg.as_bytes()
    odd = EmailMessage()
    odd["Subject"] = "odd charset"
    odd.set_content(b"Schaden \xe4nderung EUR 5m", maintype="text", subtype="plain")
    odd.replace_header("Content-Type", 'text/plain; charset="unknown-8bit"')
    for name, data in (("split.eml", raw), ("odd.eml", odd.as_bytes())):
        (tmp_path / name).write_bytes(data)
    jobs = tmp_path / "jobs"
    job = job_from(jobs, tmp_path / "split.eml")
    [split] = preprocess(job)["emails"]
    [text_part] = [a for a in split["attachments"] if a["name"].startswith("text_part")]
    assert "second part" in (job.dir / text_part["path"]).read_text(encoding="utf-8")
    job = job_from(jobs, tmp_path / "odd.eml")
    [odd_mail] = preprocess(job)["emails"]
    assert "EUR 5m" in (job.dir / odd_mail["body"]).read_text(encoding="utf-8")


def test_zip_budget_is_per_job(tmp_path, monkeypatch):
    monkeypatch.setattr("pipeline.preprocess.ZIP_MAX_BYTES", 150)
    src = tmp_path / "drop"
    src.mkdir()
    for name in ("a.zip", "b.zip"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("data.txt", "x" * 100)
        (src / name).write_bytes(buf.getvalue())
    summary = preprocess(job_from(tmp_path / "jobs", src))
    assert sum(1 for r in summary["files"] if r.get("from_zip")) == 1
    assert any("too large" in w for w in summary["warnings"])


def test_html_to_text():
    html = "<html><head><style>p{}</style><title>t</title></head><body><p>Hi&nbsp;there</p>" \
           "<ul><li>one</li><li>two</li></ul><table><tr><th>K</th><th>V</th></tr><tr><td>x</td><td>1</td></tr></table></body></html>"
    text = html_to_text(html)
    assert "p{}" not in text and "Hi there" in text
    assert "- one" in text and "K | V" in text and "x | 1" in text
    assert "Loss EUR 5m" in html_to_text("<html><head><title>t<body><p>Loss EUR 5m</p>")  # unclosed head


def test_eml_html_only(tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "html only"
    msg.set_content("<p>Hello <b>world</b></p>", subtype="html")
    src = tmp_path / "h.eml"
    src.write_bytes(msg.as_bytes())
    job = job_from(tmp_path / "jobs", src)
    [mail] = preprocess(job)["emails"]
    assert "Hello world" in (job.dir / mail["body"]).read_text(encoding="utf-8")
    assert "body_html" not in mail
