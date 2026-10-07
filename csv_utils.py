"""Pure-Python helpers (no QGIS imports, so they can be unit tested)."""
import csv
import io
import os
import re
import shutil
import sqlite3
import unicodedata

MAX_SAMPLE = 64 * 1024      # bytes read to inspect a file
MAX_SAMPLE_ROWS = 25
DELIMITERS = ",;\t|"
CSV_EXTENSIONS = (".csv", ".txt", ".tsv", ".tab")

# (x names, y names, exact-only x, exact-only y, kind)
_PAIRS = (
    (("longitude", "lon", "long", "lng", "longitud"), ("latitude", "lat", "latitud"), (), (), "latlon"),
    (("easting", "eastings", "east"), ("northing", "northings", "north"), ("e",), ("n",), "en"),
    (("x", "xcoord", "xcoordinate", "xcoor"), ("y", "ycoord", "ycoordinate", "ycoor"), (), (), "xy"),
)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_DECIMAL_COMMA = re.compile(r"^\s*-?\d+,\d+\s*$")


class SampleInfo:
    def __init__(self, encoding, delimiter, header, rows, bom=False):
        self.bom = bom                # file starts with a UTF-8 byte-order mark
        self.encoding = encoding      # name understood by the QGIS provider
        self.delimiter = delimiter
        self.header = header          # list[str]
        self.rows = rows              # list[list[str]] first few data rows


def _decode(raw):
    try:
        return "UTF-8", raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # sample may simply end in the middle of a multi-byte character
        if len(raw) >= MAX_SAMPLE and exc.start >= len(raw) - 4:
            return "UTF-8", raw[:exc.start].decode("utf-8-sig")
    return "windows-1252", raw.decode("cp1252", errors="replace")


def _py_encoding(provider_encoding):
    return "utf-8-sig" if provider_encoding == "UTF-8" else "cp1252"


def detect_delimiter(text):
    try:
        found = csv.Sniffer().sniff(text[:8192], delimiters=DELIMITERS).delimiter
        if found in DELIMITERS:
            return found
    except csv.Error:
        pass
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in DELIMITERS}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def read_sample(path):
    """Inspect a CSV. Raises ValueError with a user-friendly message."""
    if not os.path.isfile(path):
        raise ValueError("not a file")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SAMPLE)
    except OSError as exc:
        raise ValueError(f"cannot read file ({exc.strerror or exc})")
    if not raw.strip():
        raise ValueError("file is empty")
    if b"\x00" in raw:
        raise ValueError("not a plain-text CSV (UTF-16/binary files are not supported)")
    encoding, text = _decode(raw)
    delimiter = detect_delimiter(text)
    try:
        parsed = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise ValueError(f"cannot parse CSV ({exc})")
    if len(parsed) < 2:
        raise ValueError("needs a header row and at least one data row")
    header = [h.strip() for h in parsed[0]]
    if len(header) < 2:
        raise ValueError("needs at least two columns")
    return SampleInfo(encoding, delimiter, header, parsed[1:1 + MAX_SAMPLE_ROWS],
                      bom=raw.startswith(b"\xef\xbb\xbf"))


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find(header, names, exact_only=()):
    norm = [_norm(h) for h in header]
    for wanted in tuple(names) + tuple(exact_only):
        if wanted in norm:
            return norm.index(wanted)
    for i, h in enumerate(header):  # token match: "X_coord", "Lat (deg)"
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", h)   # "EastingX" -> "Easting X"
        tokens = set(t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t)
        if tokens & set(names):
            return i
    return -1


def detect_xy(header):
    """Return (x_index, y_index, kind) where kind is latlon/en/xy; (-1, -1, '') if not found."""
    for xs, ys, xs_exact, ys_exact, kind in _PAIRS:
        xi, yi = _find(header, xs, xs_exact), _find(header, ys, ys_exact)
        if xi >= 0 and yi >= 0 and xi != yi:
            return xi, yi, kind
    return -1, -1, ""


def uses_decimal_comma(rows, indexes, delimiter):
    if delimiter == ",":
        return False
    for idx in indexes:
        for row in rows:
            if idx < len(row) and _DECIMAL_COMMA.match(row[idx]):
                return True
    return False


def count_rows(path, provider_encoding, delimiter):
    """Number of non-empty data rows (header excluded)."""
    total = -1
    with open(path, "r", newline="", encoding=_py_encoding(provider_encoding), errors="replace") as handle:
        for row in csv.reader(handle, delimiter=delimiter):
            if any(c.strip() for c in row):
                total += 1
    return max(total, 0)


def sanitize_filename(name, default="output"):
    """Make a string safe to use as a file name stem (no paths, no reserved names)."""
    name = unicodedata.normalize("NFC", name or "")
    name = _INVALID_CHARS.sub("_", name).strip()
    for ext in (".shp", ".gpkg"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    name = name.strip(" .")
    name = name[:100].rstrip(" .")
    if not name:
        name = default
    if name.lower() in _RESERVED:
        name = "_" + name
    return name


def unique_stem(folder, stem, extensions):
    """Return a stem for which none of folder/stem+ext exists (adds _1, _2 ...)."""
    candidate, n = stem, 0
    while any(os.path.exists(os.path.join(folder, candidate + e)) for e in extensions):
        n += 1
        candidate = f"{stem}_{n}"
    return candidate


def is_inside(folder, path):
    try:
        folder = os.path.realpath(folder)
        path = os.path.realpath(path)
        return os.path.commonpath([folder, path]) == folder
    except ValueError:
        return False


def write_clean_utf8(src, dst, encoding, bom):
    """Copy src to dst as plain UTF-8 without BOM (the QGIS provider is picky about codecs/BOMs)."""
    chunk_size = 1 << 20
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if encoding == "UTF-8":
            if bom:
                fin.read(3)
            shutil.copyfileobj(fin, fout, chunk_size)
        else:  # single-byte Windows-1252 -> UTF-8
            while True:
                chunk = fin.read(chunk_size)
                if not chunk:
                    break
                fout.write(chunk.decode("cp1252", errors="replace").encode("utf-8"))


def unique_name(name, taken):
    """Return name (or name_1, name_2 ...) not present in `taken` (a set of lower-case names)."""
    candidate, n = name, 0
    while candidate.lower() in taken:
        n += 1
        candidate = f"{name}_{n}"
    return candidate


def gpkg_layer_names(path):
    """Lower-case names of the layers/tables already inside a GeoPackage (empty set if unreadable)."""
    if not os.path.isfile(path):
        return set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT table_name FROM gpkg_contents").fetchall()
            names = {r[0].lower() for r in rows}
            # also guard against non-registered tables with the same name
            rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return names | {r[0].lower() for r in rows}
        finally:
            con.close()
    except sqlite3.Error:
        return set()
