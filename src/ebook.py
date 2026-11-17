"""Read and write EPUB metadata. Pure Python, no GTK, no calibre.

An EPUB is a zip archive. `META-INF/container.xml` points at the package
document (the OPF), whose <metadata> block holds the Dublin Core fields we care
about: title, creators, description, publisher, language, date, identifiers and
subjects. Series isn't part of Dublin Core, so it's written both the EPUB 3 way
(belongs-to-collection) and the calibre way (calibre:series), which together
cover Kobo, KOReader, Apple Books and calibre itself.

Writing never edits the archive in place: a new zip is built next to the
original (mimetype first and uncompressed, as the spec requires) and swapped in
with an atomic rename, so a crash can't leave a half-written book.
"""
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
OPF = "{%s}" % NS["opf"]
DC = "{%s}" % NS["dc"]

# Keep the conventional prefixes when ElementTree re-serialises the OPF. The OPF
# namespace goes out as "opf:" (ElementTree needs a prefix for opf:role-style
# attributes) and _serialise() turns the element names back into the usual
# default-namespace form.
ET.register_namespace("dc", NS["dc"])
ET.register_namespace("opf", NS["opf"])
ET.register_namespace("dcterms", "http://purl.org/dc/terms/")
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

SUFFIXES = (".epub",)
AUTHOR_SEP = " & "


class EbookError(Exception):
    pass


# ---------- locating the package document ----------

def _opf_path(zf):
    try:
        root = ET.fromstring(zf.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError) as e:
        raise EbookError("Not a valid EPUB (no container.xml)") from e
    rootfile = root.find(".//c:rootfile", NS)
    if rootfile is None or not rootfile.get("full-path"):
        raise EbookError("Not a valid EPUB (no package document)")
    return rootfile.get("full-path")


def _metadata(root):
    meta = root.find(OPF + "metadata")
    if meta is None:
        meta = ET.SubElement(root, OPF + "metadata")
        root.remove(meta)
        root.insert(0, meta)
    return meta


def _text(el):
    return (el.text or "").strip() if el is not None else ""


# ---------- reading ----------

def split_authors(author):
    """Quill stores authors as one string; EPUB wants one creator each."""
    parts = re.split(r"\s*(?:&|;|\band\b)\s*", author or "")
    return [p.strip() for p in parts if p.strip()]


def _isbn_from(meta):
    for ident in meta.findall(DC + "identifier"):
        value = _text(ident)
        scheme = (ident.get(OPF + "scheme") or ident.get("scheme") or "").lower()
        digits = re.sub(r"[^0-9Xx]", "", value.split(":")[-1])
        if scheme == "isbn" or value.lower().startswith("urn:isbn:") or (
                len(digits) in (10, 13) and digits[:-1].isdigit()):
            if len(digits) in (10, 13):
                return digits.upper()
    return ""


def _series_from(meta):
    series, index = "", 0.0
    for m in meta.findall(OPF + "meta"):
        name = m.get("name")
        if name == "calibre:series":
            series = m.get("content", "").strip() or series
        elif name == "calibre:series_index":
            index = _float(m.get("content")) or index
    if not series:
        for m in meta.findall(OPF + "meta"):
            if m.get("property") == "belongs-to-collection":
                series = _text(m)
                ref = "#" + (m.get("id") or "")
                for r in meta.findall(OPF + "meta"):
                    if r.get("refines") == ref and r.get("property") == "group-position":
                        index = _float(r.text) or index
                break
    return series, index


def _float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _cover_href(root):
    manifest = root.find(OPF + "manifest")
    if manifest is None:
        return None
    items = manifest.findall(OPF + "item")
    for item in items:  # EPUB 3
        if "cover-image" in (item.get("properties") or "").split():
            return item.get("href")
    meta = root.find(OPF + "metadata")
    if meta is not None:  # EPUB 2: <meta name="cover" content="item-id"/>
        for m in meta.findall(OPF + "meta"):
            if m.get("name") == "cover":
                for item in items:
                    if item.get("id") == m.get("content"):
                        return item.get("href")
    for item in items:  # last resort: an image that calls itself a cover
        if (item.get("media-type") or "").startswith("image/") and \
                "cover" in ((item.get("id") or "") + (item.get("href") or "")).lower():
            return item.get("href")
    return None


def read(path):
    """Return a dict of the EPUB's metadata plus its cover image bytes (or None)
    and the cover's file extension."""
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _opf_path(zf)
            root = ET.fromstring(zf.read(opf_name))
            meta = _metadata(root)
            series, series_index = _series_from(meta)
            date = _text(meta.find(DC + "date"))
            year = int(date[:4]) if date[:4].isdigit() else 0
            data = {
                "title": _text(meta.find(DC + "title")),
                "author": AUTHOR_SEP.join(
                    _text(c) for c in meta.findall(DC + "creator") if _text(c)),
                "description": _strip_html(_text(meta.find(DC + "description"))),
                "publisher": _text(meta.find(DC + "publisher")),
                "language": _text(meta.find(DC + "language")),
                "year": year,
                "isbn": _isbn_from(meta),
                "tags": ", ".join(
                    _text(s) for s in meta.findall(DC + "subject") if _text(s)),
                "series": series,
                "series_index": series_index,
                "cover": None,
                "cover_ext": "",
            }
            href = _cover_href(root)
            if href:
                cover_name = _join(os.path.dirname(opf_name), href)
                try:
                    data["cover"] = zf.read(cover_name)
                    data["cover_ext"] = os.path.splitext(cover_name)[1].lower() or ".jpg"
                except KeyError:
                    pass
            return data
    except (zipfile.BadZipFile, ET.ParseError, OSError) as e:
        raise EbookError(f"Couldn't read {os.path.basename(path)}") from e


def _join(base, href):
    from urllib.parse import unquote
    return os.path.normpath(os.path.join(base, unquote(href))).replace(os.sep, "/")


def _strip_html(text):
    text = re.sub(r"<\s*(br|/p|/div)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# ---------- writing ----------

def _set_single(meta, tag, value):
    """Replace every <tag> with one holding `value` (or none if empty)."""
    existing = meta.findall(tag)
    first_index = list(meta).index(existing[0]) if existing else len(meta)
    for el in existing:
        meta.remove(el)
    if value:
        el = ET.Element(tag)
        el.text = value
        meta.insert(first_index, el)
        return el
    return None


def _set_multi(meta, tag, values):
    existing = meta.findall(tag)
    first_index = list(meta).index(existing[0]) if existing else len(meta)
    old = {_text(el): dict(el.attrib) for el in existing}
    role = {k: v for k, v in (dict(existing[0].attrib) if existing else {}).items()
            if k == OPF + "role"}
    for el in existing:
        meta.remove(el)
    made = []
    for i, value in enumerate(values):
        # An unchanged value keeps its attributes (id for refines, file-as);
        # a new one only inherits the role, never another person's sort name.
        attrs = old.get(value, role)
        if any(a.get("id") and a.get("id") == attrs.get("id")
               for a in (m.attrib for m in made)):
            attrs = {k: v for k, v in attrs.items() if k != "id"}
        el = ET.Element(tag, attrs)
        el.text = value
        meta.insert(first_index + i, el)
        made.append(el)
    return made


def _set_date_year(meta, year):
    el = meta.find(DC + "date")
    if not year:
        return
    current = _text(el)
    if current[:4] == str(year):
        return  # keep a fuller date like 2019-05-14 when the year matches
    _set_single(meta, DC + "date", str(year))


def _set_isbn(meta, isbn):
    isbn = re.sub(r"[^0-9Xx]", "", isbn or "").upper()
    if not isbn or _isbn_from(meta) == isbn:
        return
    for ident in meta.findall(DC + "identifier"):
        if _isbn_from_one(ident):
            ident.text = f"urn:isbn:{isbn}"
            return
    el = ET.SubElement(meta, DC + "identifier")
    el.text = f"urn:isbn:{isbn}"


def _isbn_from_one(ident):
    holder = ET.Element(OPF + "metadata")
    holder.append(ident)
    return bool(_isbn_from(holder))


def _set_series(meta, series, index, epub3=True):
    for m in list(meta.findall(OPF + "meta")):
        if m.get("name") in ("calibre:series", "calibre:series_index"):
            meta.remove(m)
    coll_ids = set()
    for m in list(meta.findall(OPF + "meta")):
        if m.get("property") == "belongs-to-collection":
            coll_ids.add("#" + (m.get("id") or ""))
            meta.remove(m)
    for m in list(meta.findall(OPF + "meta")):
        if m.get("refines") in coll_ids:
            meta.remove(m)
    if not series:
        return
    idx = f"{index:g}" if index else "1"
    ET.SubElement(meta, OPF + "meta", {"name": "calibre:series", "content": series})
    ET.SubElement(meta, OPF + "meta", {"name": "calibre:series_index", "content": idx})
    if not epub3:
        return  # EPUB 2 has no refines/property metadata
    coll = ET.SubElement(meta, OPF + "meta",
                         {"property": "belongs-to-collection", "id": "quill-series"})
    coll.text = series
    ET.SubElement(meta, OPF + "meta", {"refines": "#quill-series",
                                       "property": "collection-type"}).text = "series"
    ET.SubElement(meta, OPF + "meta", {"refines": "#quill-series",
                                       "property": "group-position"}).text = idx


def write(path, *, title, author="", description="", publisher="", language="",
          year=0, isbn="", tags="", series="", series_index=0.0):
    """Write the given metadata into the EPUB at `path`, replacing those fields.
    Everything else in the book (content, styling, other metadata) is kept."""
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _opf_path(zf)
            raw = zf.read(opf_name)
            root = ET.fromstring(raw)
            meta = _metadata(root)

            _set_single(meta, DC + "title", title)
            _set_multi(meta, DC + "creator", split_authors(author))
            _set_single(meta, DC + "description", description)
            _set_single(meta, DC + "publisher", publisher)
            if language:
                _set_single(meta, DC + "language", language)
            _set_date_year(meta, year)
            _set_isbn(meta, isbn)
            _set_multi(meta, DC + "subject",
                       [t.strip() for t in (tags or "").split(",") if t.strip()])
            _set_series(meta, series, series_index,
                        epub3=(root.get("version") or "").startswith("3"))

            new_opf = _serialise(root)
            folder = os.path.dirname(os.path.abspath(path))
            fd, tmp = tempfile.mkstemp(suffix=".epub", dir=folder)
            os.close(fd)
            try:
                with zipfile.ZipFile(tmp, "w") as out:
                    names = zf.namelist()
                    if "mimetype" in names:
                        out.writestr(zipfile.ZipInfo("mimetype"), zf.read("mimetype"),
                                     compress_type=zipfile.ZIP_STORED)
                    for info in zf.infolist():
                        if info.filename == "mimetype":
                            continue
                        data = new_opf if info.filename == opf_name else zf.read(info)
                        out.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
    except (zipfile.BadZipFile, ET.ParseError) as e:
        raise EbookError(f"Couldn't update {os.path.basename(path)}") from e


def _serialise(root):
    xml = ET.tostring(root, encoding="unicode")
    xml = re.sub(r"<(/?)opf:", r"<\1", xml)
    xml = xml.replace("<package ", f'<package xmlns="{NS["opf"]}" ', 1)
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + xml).encode("utf-8")


# ---------- file naming ----------

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(text, fallback="Unknown", limit=90):
    text = _BAD.sub("_", (text or "").strip()).strip(". ")
    return (text[:limit].rstrip(". ") or fallback)


def library_relpath(title, author, ext=".epub"):
    """Where a book lives inside the ebook folder: Author/Title.epub."""
    first_author = (split_authors(author) or ["Unknown author"])[0]
    return os.path.join(safe_name(first_author, "Unknown author"),
                        safe_name(title, "Untitled") + ext)


def device_filename(title, author, series="", series_index=0.0, ext=".epub"):
    """A flat, readable name for an e-reader: Author - Series 02 - Title.epub."""
    first_author = (split_authors(author) or ["Unknown author"])[0]
    parts = [safe_name(first_author, "Unknown author", 60)]
    if series:
        num = f" {int(series_index):02d}" if series_index and \
            float(series_index).is_integer() else (f" {series_index:g}" if series_index else "")
        parts.append(safe_name(series, "", 60) + num)
    parts.append(safe_name(title, "Untitled", 90))
    return " - ".join(p for p in parts if p) + ext
