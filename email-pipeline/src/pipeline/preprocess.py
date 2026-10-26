"""Deterministic preprocessing: no LLM here.

Turns whatever is in job/original into clean inputs in job/extracted:
    extracted/<email>/body.txt              headers + plain-text body
    extracted/<email>/body_html.txt         HTML body as text with table rows kept
                                            (only when the HTML body contains tables)
    extracted/<email>/attachments/<files>   attachments, with text versions next to them:
        <workbook>__<sheet>.csv             one CSV per sheet (.xlsx/.xlsm/.xls/.xlsb)
        <document>.txt                      text of .pdf/.docx (PDFs stay readable directly)
    extracted/files/<files>                 items dropped without an email
    extracted/summary.json                  index of everything above

Emails attached to emails (forward-as-attachment) and .zip archives are
expanded. Every email gets its own folder directly under extracted/ (flat, to
keep Windows paths under 260 characters). All paths in summary.json are
relative to the job directory, which is the route agents' working directory.
"""
from __future__ import annotations

import email
import json
import logging
import re
import shutil
import zipfile
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

from .jobs import Job, safe_name, unique_path

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # very chatty about malformed PDFs

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".emz", ".wmz"}
MAX_DEPTH = 4                 # emails/zips nested inside each other
ZIP_MAX_FILES = 500           # per job, across all (nested) archives
ZIP_MAX_BYTES = 300 * 2**20   # per job, declared uncompressed size
RESERVED = {"files", "summary.json"}


def preprocess(job: Job) -> dict:
    ex = _Extractor(job)
    for f in sorted(p for p in job.original_dir.rglob("*") if p.is_file()):
        try:
            if f.suffix.lower() == ".msg":
                ex.msg_file(f, ex.rel(f), depth=0)
            elif f.suffix.lower() == ".eml":
                ex.eml(email.message_from_bytes(f.read_bytes(), policy=policy.default), ex.rel(f), depth=0)
            else:
                ex.summary["files"].extend(ex.store(f.name, f.read_bytes(), ex.out / "files", depth=0))
        except Exception as exc:  # keep going; record what failed
            log.exception("preprocess failed for %s", f)
            ex.warn(f"could not process {f.name}: {exc}")
    (ex.out / "summary.json").write_text(json.dumps(ex.summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return ex.summary


class _Extractor:
    def __init__(self, job: Job):
        self.job = job
        self.out = job.extracted_dir
        if self.out.exists():  # re-run (retry): extracted/ is fully derived
            shutil.rmtree(self.out)
        self.out.mkdir()
        self.summary: dict = {"emails": [], "files": [], "warnings": []}
        self.unzip_bytes_left = ZIP_MAX_BYTES
        self.unzip_files_left = ZIP_MAX_FILES

    def rel(self, p: Path) -> str:
        return p.relative_to(self.job.dir).as_posix()

    def warn(self, text: str) -> None:
        self.summary["warnings"].append(text)

    # ---- emails -----------------------------------------------------------

    def add_email(self, source: str, headers: dict, plain: str, html: str, parent: dict | None) -> tuple[dict, Path]:
        headers = {k: "" if v is None else str(v) for k, v in headers.items()}
        name = safe_name(headers.get("Subject") or Path(source).stem, 40)
        dest = unique_path(self.out, f"email_{name}" if name.lower() in RESERVED else name)
        dest.mkdir(parents=True)
        head = "\n".join(f"{k}: {v}" for k, v in headers.items())
        html_text = html_to_text(html) if html else ""
        has_plain = bool(plain and plain.strip())
        body = _clean(plain) if has_plain else html_text
        (dest / "body.txt").write_text(f"{head}\n\n{body}", encoding="utf-8")
        record = {
            "source": source,
            "embedded_in": parent["body"] if parent else None,
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
            "date": headers.get("Date", ""),
            "body": self.rel(dest / "body.txt"),
            "attachments": [],
        }
        if has_plain and html_text and "<table" in html.lower():  # plain text loses table layout
            (dest / "body_html.txt").write_text(f"{head}\n\n{html_text}", encoding="utf-8")
            record["body_html"] = self.rel(dest / "body_html.txt")
        self.summary["emails"].append(record)
        return record, dest

    def msg_file(self, path: Path, source: str, depth: int, parent: dict | None = None) -> dict:
        import extract_msg  # imported lazily so tests don't need it
        from extract_msg.enums import ErrorBehavior

        # SUPPRESS_ALL: an odd attachment or malformed RTF must not make the whole email unreadable
        msg = extract_msg.openMsg(str(path), strict=False, errorBehavior=ErrorBehavior.SUPPRESS_ALL)
        try:
            return self.msg(msg, source, depth, parent)
        finally:
            close = getattr(msg, "close", None)
            if close:
                close()

    def _attr(self, obj, name: str, source: str):
        """A property of an extract_msg object; these parse lazily and can raise."""
        try:
            return getattr(obj, name, None)
        except Exception as exc:
            log.warning("%s of %s: %s", name, source, exc)
            self.warn(f"could not read {name} of {source}: {exc}")
            return None

    def msg(self, msg, source: str, depth: int, parent: dict | None = None) -> dict:
        headers = {k: self._attr(msg, attr, source) for k, attr in
                   (("From", "sender"), ("To", "to"), ("Cc", "cc"), ("Date", "date"), ("Subject", "subject"))}
        html = self._attr(msg, "htmlBody", source)
        record, dest = self.add_email(source, headers, self._attr(msg, "body", source) or "",
                                      _decode_html(html) if html else "", parent)
        for att in self._attr(msg, "attachments", source) or []:
            name = (getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
                    or getattr(att, "name", None) or "attachment")
            try:
                self._msg_attachment(att, name, record, dest, source, depth)
            except Exception as exc:  # one broken attachment must not lose the email
                log.warning("attachment %s in %s: %s", name, source, exc)
                self.warn(f"attachment '{name}' in {source} could not be read: {exc}")
        return record

    def _msg_attachment(self, att, name: str, record: dict, dest: Path, source: str, depth: int) -> None:
        data = getattr(att, "data", None)
        if isinstance(data, (bytes, bytearray)):
            inline = bool(getattr(att, "cid", None)) and Path(name).suffix.lower() in IMAGE_SUFFIXES
            record["attachments"].extend(self.store(name, bytes(data), dest / "attachments", depth, record, inline))
        elif hasattr(data, "attachments"):  # an email attached to the email
            if depth >= MAX_DEPTH:
                self.warn(f"'{name}' in {source} nested too deep, not expanded")
                return
            subject = getattr(data, "subject", None) or name
            child = self.msg(data, f"{source} > {subject}", depth + 1, record)
            record["attachments"].append({"name": subject, "email": child["body"]})
        else:
            self.warn(f"attachment '{name}' in {source} skipped (unsupported type)")

    def eml(self, msg: EmailMessage, source: str, depth: int, parent: dict | None = None) -> dict:
        plain_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        plain = _text_of(plain_part) if plain_part else ""
        html = _text_of(html_part) if html_part else ""
        headers = {k: msg.get(k, "") for k in ("From", "To", "Cc", "Date", "Subject")}
        record, dest = self.add_email(source, headers, plain, html, parent)
        for part in _eml_attachments(msg, skip=[plain_part, html_part]):
            try:
                self._eml_part(part, record, dest, source, depth)
            except Exception as exc:  # one broken attachment must not lose the email
                log.warning("attachment %s in %s: %s", part.get_filename(), source, exc)
                self.warn(f"attachment '{part.get_filename() or part.get_content_type()}' in {source} could not be read: {exc}")
        return record

    def _eml_part(self, part: EmailMessage, record: dict, dest: Path, source: str, depth: int) -> None:
        if part.get_content_type() == "message/rfc822":
            if depth >= MAX_DEPTH:
                self.warn(f"attached email in {source} nested too deep, not expanded")
                return
            inner = part.get_content()
            child = self.eml(inner, f"{source} > {inner.get('Subject', 'attached email')}", depth + 1, record)
            record["attachments"].append({"name": str(inner.get("Subject", "")), "email": child["body"]})
            return
        payload = part.get_payload(decode=True)
        if not payload:
            return
        inline = bool(part.get("Content-ID")) and part.get_content_maintype() == "image"
        default = "text_part" if part.get_content_maintype() == "text" else "attachment"
        name = part.get_filename() or f"{default}{_guess_ext(part.get_content_type())}"
        record["attachments"].extend(self.store(name, payload, dest / "attachments", depth, record, inline))

    # ---- files ------------------------------------------------------------

    def store(self, name: str, data: bytes, dest: Path, depth: int,
              parent: dict | None = None, inline: bool = False) -> list[dict]:
        """Save one file and its derived versions. Returns its summary record(s)."""
        dest.mkdir(parents=True, exist_ok=True)
        target = unique_path(dest, safe_name(name, 60))
        target.write_bytes(data)
        record: dict = {"name": name, "path": self.rel(target), "bytes": len(data)}
        if inline:
            record["inline"] = True
        records = [record]
        suffix = target.suffix.lower()
        try:
            if suffix in EXCEL_SUFFIXES:
                record["derived"] = [self.rel(p) for p in _excel_to_csv(target)]
            elif suffix == ".pdf":
                txt, has_text = _pdf_to_text(target)
                record["derived"] = [self.rel(txt)]
                if not has_text:
                    self.warn(f"{name}: PDF has no text layer (scanned?); agents can still read the PDF itself")
            elif suffix == ".docx":
                record["derived"] = [self.rel(_docx_to_text(target))]
            elif suffix in (".msg", ".eml", ".zip") and depth >= MAX_DEPTH:
                self.warn(f"{name} nested too deep, not expanded")
            elif suffix == ".msg":
                record["email"] = self.msg_file(target, f"{self.rel(target)}", depth + 1, parent)["body"]
            elif suffix == ".eml":
                inner = email.message_from_bytes(data, policy=policy.default)
                record["email"] = self.eml(inner, self.rel(target), depth + 1, parent)["body"]
            elif suffix == ".zip":
                records.extend(self.unzip(target, depth + 1, parent))
        except Exception as exc:
            log.warning("could not convert %s: %s", target, exc)
            self.warn(f"could not convert {name}: {exc}")
        return records

    def unzip(self, path: Path, depth: int, parent: dict | None) -> list[dict]:
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            size = sum(i.file_size for i in infos)
            if len(infos) > self.unzip_files_left or size > self.unzip_bytes_left:  # budget for the whole job
                self.warn(f"{path.name}: archive too large, not expanded")
                return []
            self.unzip_files_left -= len(infos)
            self.unzip_bytes_left -= size
            root = unique_path(path.parent, safe_name(path.stem, 40) + "_unzipped")
            records = []
            for info in infos:
                # safe_name flattens folders into the file name: no zip-slip, short paths
                for r in self.store(info.filename, zf.read(info), root, depth, parent):
                    r["from_zip"] = self.rel(path)
                    records.append(r)
            return records


def _eml_attachments(part: EmailMessage, skip: list) -> list[EmailMessage]:
    """Every leaf part except the chosen body parts, without descending into attached
    emails. Extra text parts (some mail clients split the body around attachments)
    are kept as attachments rather than silently dropped."""
    if part.get_content_type() == "message/rfc822":
        return [part]
    if part.is_multipart():
        return [a for sub in part.iter_parts() for a in _eml_attachments(sub, skip)]
    return [] if any(part is s for s in skip) else [part]


def _text_of(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, ValueError):  # unknown or wrong charset, e.g. "unknown-8bit"
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def _guess_ext(content_type: str) -> str:
    import mimetypes

    return mimetypes.guess_extension(content_type) or ".bin"


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


def _decode_html(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    m = re.search(rb'charset=["\']?([\w-]+)', data[:4096], re.IGNORECASE)
    for enc in ([m.group(1).decode("ascii")] if m else []) + ["utf-8"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("cp1252", errors="replace")


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "hr", "pre", "section", "article", "header", "footer"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip = 0  # HTML5 allows <head>/<title> without closing tags
        elif tag in self.SKIP:
            self.skip += 1
        elif tag == "tr":
            self.out.append("\n")
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.out.append(" | ")
            self.cells += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag == "pre":
                self.pre += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK or tag == "tr":
            self.out.append("\n")
            if tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    lines = [line.strip() for line in _clean("".join(parser.out)).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip() + "\n"


def _excel_to_csv(path: Path) -> list[Path]:
    import pandas as pd

    engine = "pyxlsb" if path.suffix.lower() == ".xlsb" else None
    out = []
    for sheet, df in pd.read_excel(path, sheet_name=None, header=None, engine=engine).items():
        if df.dropna(how="all").empty:
            continue
        csv = unique_path(path.parent, f"{safe_name(path.stem, 60)}__{safe_name(str(sheet), 31)}.csv")
        df.to_csv(csv, index=False, header=False)
        out.append(csv)
    return out


def _pdf_to_text(path: Path) -> tuple[Path, bool]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        reader.decrypt("")  # many "protected" PDFs only restrict printing/copying
    pages = [f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages, 1)]
    target = path.with_name(path.name + ".txt")
    target.write_text("\n\n".join(pages), encoding="utf-8")
    has_text = len(re.sub(r"--- page \d+ ---|\s", "", "".join(pages))) >= 20
    return target, has_text


def _docx_to_text(path: Path) -> Path:
    import docx

    doc = docx.Document(str(path))
    lines = []
    for block in doc.iter_inner_content():  # paragraphs and tables in document order
        if hasattr(block, "rows"):
            lines.extend(" | ".join(c.text.strip() for c in row.cells) for row in block.rows)
        else:
            lines.append(block.text)
    target = path.with_name(path.name + ".txt")
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
