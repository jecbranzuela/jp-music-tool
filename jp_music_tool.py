"""JP Music Tool

1. Renames audio files with Japanese names to  "English translation (romanization).ext"
2. Embeds a folder's cover image into every audio file (FLAC, MP3, M4A, OGG/Opus, WAV, AIFF)

Requires: mutagen, pykakasi, cutlet, unidic-lite, deep-translator, pillow, sv-ttk
(install: pip install -r requirements.txt)
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aac", ".alac", ".ogg", ".oga", ".opus",
              ".wav", ".aif", ".aiff", ".wma", ".ape", ".wv", ".dsf"}
SIDECAR_EXTS = [".lrc"]  # renamed together with the audio file if the stem matches
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
COVER_NAMES = ["cover", "folder", "front", "album"]
LOG_NAME = "_rename_log.json"
MAX_PATH = 240

JP_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ々〆ー]")
# "01 ", "01. ", "01 - ", "1-01 ", "1-01 - ", "01_", "01)"
PREFIX_RE = re.compile(r"^(\s*(?:\d{1,2}[-.])?\d{1,3}(?:\s*[-._)]\s*|\s+))(?=\S)")

PARTICLES = {"no", "ni", "wa", "wo", "o", "ga", "de", "to", "e", "mo", "ya", "ka", "yo", "ne", "na"}
SMALL_EN = {"a", "an", "the", "of", "in", "on", "to", "and", "or", "but", "for", "at", "by",
            "with", "from", "as", "into", "nor"}


def has_japanese(text: str) -> bool:
    return bool(JP_RE.search(text))


# ---------------------------------------------------------------- romanization
_kks = None
_cut = False  # False = not tried yet, None = unavailable
KATA_RUN_RE = re.compile(r"([ァ-ヺーヽヾｦ-ﾟ]+)")


def _kakasi():
    global _kks
    if _kks is None:
        import pykakasi
        _kks = pykakasi.kakasi()
    return _kks


def _cutlet():
    """cutlet (MeCab-based) gives much better readings than pykakasi; optional."""
    global _cut
    if _cut is False:
        try:
            import cutlet
            _cut = cutlet.Cutlet()
            _cut.use_foreign_spelling = False
        except Exception:
            _cut = None
    return _cut


def _romanize_kakasi(text: str) -> str:
    parts = []
    for tok in _kakasi().convert(text):
        orig, rom = tok["orig"], tok["hepburn"]
        if orig == "は":      # topic particle
            rom = "wa"
        elif orig == "へ":    # direction particle
            rom = "e"
        parts.append(rom if has_japanese(orig) else orig)
    return " ".join(p.strip() for p in parts if p.strip())


def _romanize_katakana(run: str) -> str:
    # Katakana words (often loanwords/names) are read as one word: ハルジオン -> harujion
    return "".join(t["hepburn"] for t in _kakasi().convert(run))


def romanize(text: str) -> str:
    cut = _cutlet()
    if cut is None:
        out = _romanize_kakasi(text)
    else:
        parts = []
        for chunk in KATA_RUN_RE.split(text):
            if not chunk:
                continue
            if KATA_RUN_RE.fullmatch(chunk):
                parts.append(_romanize_katakana(chunk))
            elif has_japanese(chunk):
                parts.append(cut.romaji(chunk, capitalize=False))
            else:
                parts.append(chunk)
        out = " ".join(p.strip() for p in parts if p.strip())
    out = unicodedata.normalize("NFKC", out)
    out = re.sub(r"\s+([,.!?;:)\]」』])", r"\1", out)
    out = re.sub(r"([(\[「『])\s+", r"\1", out)
    out = re.sub(r"\s+", " ", out).strip()
    words = out.split(" ")
    return " ".join(
        w if (i > 0 and w.lower().rstrip(".,!?~") in PARTICLES) else (w[:1].upper() + w[1:])
        for i, w in enumerate(words)
    )


# ---------------------------------------------------------------- translation
_tr_cache: dict[str, str] = {}


def _translate_gtx(text: str) -> str:
    import urllib.parse
    import urllib.request
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl=en&dt=t&q="
           + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def translate(text: str) -> str:
    """Google Translate ja->en. Raises on network failure."""
    if text in _tr_cache:
        return _tr_cache[text]
    try:
        res = _translate_gtx(text)
    except Exception:
        from deep_translator import GoogleTranslator  # fallback endpoint
        res = GoogleTranslator(source="ja", target="en").translate(text) or ""
    time.sleep(0.25)  # be polite: stay well under Google's rate limit
    res = unicodedata.normalize("NFKC", res).strip().strip('"').strip()
    res = title_case_en(res)
    _tr_cache[text] = res
    return res


def title_case_en(s: str) -> str:
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        if not w:
            continue
        if 0 < i < len(words) - 1 and w.lower() in SMALL_EN:
            out.append(w.lower())
        elif w[:1].islower():
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def sanitize(name: str) -> str:
    repl = {":": " -", "/": "-", "\\": "-", "|": "-", "?": "", "*": "", "<": "", ">": "", '"': "'"}
    for k, v in repl.items():
        name = name.replace(k, v)
    name = "".join(c for c in name if ord(c) >= 32)
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(". ")


# ---------------------------------------------------------------- rename planning
@dataclass
class RenameItem:
    src: Path
    new_name: str
    note: str = ""
    sidecars: list[Path] = field(default_factory=list)


def iter_dirs(root: Path, recursive: bool):
    if recursive:
        for d, _, _ in os.walk(root):
            yield Path(d)
    else:
        yield root


FORMATS = {"English (Romaji)": "both", "Romaji only": "romaji", "English only": "english"}
BRACKET_PREFIX_RE = re.compile(r"^(\s*[\[(][^\])]*[\])]\s*(?:-\s*)?)(?=\S)")  # "[2020] ", "(Disc 1) - "


def split_prefix(text: str) -> tuple[str, str]:
    """Split off a leading track number / [year] tag that should be kept as-is."""
    m = PREFIX_RE.match(text) or BRACKET_PREFIX_RE.match(text)
    if m and not has_japanese(m.group(1)):
        return m.group(1), text[len(m.group(1)):]
    return "", text


def convert_text(text: str, mode: str = "both", keep_prefix: bool = False) -> tuple[str, str]:
    """Japanese text -> 'English (Romaji)' / 'Romaji' / 'English'. Returns (result, note)."""
    prefix, title = split_prefix(text) if keep_prefix else ("", text)
    note = ""
    romaji = romanize(title)
    english = ""
    if mode in ("both", "english"):
        try:
            english = translate(title)
        except Exception as e:  # offline / rate-limited
            note = f"translation failed ({type(e).__name__}); romaji only"
    if english and has_japanese(english):
        english = ""
    if mode == "romaji" or not english:
        body = romaji
    elif mode == "english" or _norm(english) == _norm(romaji):
        body = english
    else:
        body = f"{english} ({romaji})"
    return prefix + body, note


def build_name(stem: str, ext: str, use_translation: bool = True) -> tuple[str, str]:
    """Return (new filename, note)."""
    text, note = convert_text(stem, "both" if use_translation else "romaji", keep_prefix=True)
    return sanitize(text) + ext, note


def plan_renames(root: Path, recursive: bool, progress=None, use_translation=True) -> list[RenameItem]:
    files = []
    for d in iter_dirs(root, recursive):
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                m = PREFIX_RE.match(p.stem)
                title = p.stem[len(m.group(1)):] if m else p.stem
                if has_japanese(title):
                    files.append(p)
    items = []
    for i, p in enumerate(files, 1):
        if progress:
            progress(i, len(files), p.name)
        name, note = build_name(p.stem, p.suffix, use_translation)
        sidecars = [p.with_suffix(e) for e in SIDECAR_EXTS if p.with_suffix(e).exists()]
        items.append(RenameItem(p, name, note, sidecars))
    return items


def _unique_target(folder: Path, name: str, taken: set[str], self_path: Path) -> Path:
    stem, ext = (name, "") if self_path.is_dir() else os.path.splitext(name)
    room = MAX_PATH - len(str(folder)) - len(ext) - 6
    if len(stem) > room:
        stem = stem[:max(room, 10)].rstrip(". ")
    cand, n = stem + ext, 2
    while True:
        target = folder / cand
        key = str(target).lower()
        clash = key in taken or (target.exists() and target.resolve() != self_path.resolve())
        if not clash:
            taken.add(key)
            return target
        cand = f"{stem} ({n}){ext}"
        n += 1


def title_from_name(name: str) -> str:
    """'01 Run into the Night (Yoru ni Kakeru).flac' -> 'Run into the Night (Yoru ni Kakeru)'"""
    stem = os.path.splitext(name)[0]
    return split_prefix(stem)[1].strip()


def apply_renames(root: Path, items: list[RenameItem], log=print,
                  set_title: bool = False) -> list[tuple[str, str]]:
    taken: set[str] = set()
    done: list[tuple[str, str]] = []
    tag_log: list = []
    for it in items:
        if it.new_name == it.src.name or not it.src.exists():
            continue
        new_name = sanitize(os.path.splitext(it.new_name)[0]) + it.src.suffix
        if set_title:  # written before the rename, so undo (rename back, then restore tag) lines up
            title = title_from_name(new_name)
            try:
                old_vals = get_tag(it.src, "title")
                if title and old_vals != [title]:
                    set_tag(it.src, "title", [title])
                    tag_log.append([_rel(root, str(it.src)), "title", old_vals])
            except Exception as e:
                log(f"FAILED: title tag on {it.src.name}: {e}")
        target = _unique_target(it.src.parent, new_name, taken, it.src)
        try:
            os.rename(it.src, target)
            done.append((str(it.src), str(target)))
            log(f"Renamed: {it.src.name}  ->  {target.name}")
            for sc in it.sidecars:
                sc_target = target.with_suffix(sc.suffix)
                if sc.exists() and not sc_target.exists():
                    os.rename(sc, sc_target)
                    done.append((str(sc), str(sc_target)))
        except OSError as e:
            log(f"FAILED: {it.src.name}: {e}")
    if done or tag_log:
        _append_log(root, done, tag_log)
    if tag_log:
        log(f"Title tag updated in {len(tag_log)} file(s).")
    return done


def _rel(root: Path, p: str) -> str:
    return os.path.relpath(p, root)


def _append_log(root: Path, done, tags=None):
    path = root / LOG_NAME
    data = _read_log(root)
    data.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "renames": [[_rel(root, a), _rel(root, b)] for a, b in done],
                 "tags": tags or []})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_log(root: Path) -> list:
    path = root / LOG_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return []


def _write_log(root: Path, data: list):
    path = root / LOG_NAME
    if data:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def undo_last(root: Path, log=print) -> Path:
    """Reverse the latest batch (file renames, tag edits, folder renames).
    Returns the root folder path afterwards (it changes if the root itself was renamed back)."""
    data = _read_log(root)
    if not data:
        log("Nothing to undo in this folder.")
        return root
    batch = data.pop()
    (root / LOG_NAME).unlink(missing_ok=True)
    count = 0
    # 1. the selected folder itself (renamed last, so undone first)
    if batch.get("root_rename"):
        old_name, new_name = batch["root_rename"]
        target = root.parent / old_name
        if root.name == new_name and not target.exists():
            os.rename(root, target)
            root = target
            count += 1
            log(f"Restored folder: {new_name}  ->  {old_name}")
        else:
            log(f"Skipped (changed since): folder {new_name}")
    # 2. file / sub-folder renames
    for old, new in reversed(batch.get("renames", [])):
        o, n = root / old, root / new
        if n.exists() and not o.exists():
            os.rename(n, o)
            count += 1
            log(f"Restored: {n.name}  ->  {o.name}")
        else:
            log(f"Skipped (changed since): {new}")
    # 3. tag edits
    for rel, fld, old_vals in reversed(batch.get("tags", [])):
        try:
            set_tag(root / rel, fld, old_vals)
            count += 1
            log(f"Restored {FIELD_LABELS.get(fld, fld).lower()}: {Path(rel).name}  ->  {'; '.join(old_vals)}")
        except Exception as e:
            log(f"FAILED to restore tag on {rel}: {e}")
    _write_log(root, data)
    log(f"Undo complete: {count} change(s) reversed (batch from {batch['time']}).")
    return root


# ---------------------------------------------------------------- album / artist / folder
FIELD_LABELS = {"folder": "Folder name", "album": "Album", "artist": "Artist", "albumartist": "Album artist",
                "title": "Title"}
ID3_FRAMES = {"album": "TALB", "artist": "TPE1", "albumartist": "TPE2", "title": "TIT2"}


def _is_id3_container(f) -> bool:
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
    return isinstance(f, (WAVE, AIFF))


def get_tag(path: Path, fld: str) -> list[str]:
    import mutagen
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return []
    if _is_id3_container(f):
        fr = f.tags.get(ID3_FRAMES[fld])
        return [str(t) for t in fr.text] if fr else []
    try:
        return [str(v) for v in f.tags.get(fld, [])]
    except (KeyError, ValueError):
        return []


def set_tag(path: Path, fld: str, values: list[str]):
    import mutagen
    import mutagen.id3
    f = mutagen.File(path, easy=True)
    if f is None:
        raise ValueError("unsupported file")
    if f.tags is None:
        f.add_tags()
    if _is_id3_container(f):
        code = ID3_FRAMES[fld]
        f.tags.delall(code)
        if values:
            f.tags.add(getattr(mutagen.id3, code)(encoding=3, text=values))
    else:
        if values:
            f.tags[fld] = values
        elif fld in f.tags:
            del f.tags[fld]
    try:
        f.save(v2_version=3)  # MP3 / WAV / AIFF: ID3 v2.3 for Windows Explorer
    except TypeError:
        f.save()


@dataclass
class MetaItem:
    folder: Path
    fld: str            # folder / album / artist / albumartist
    old: str
    new: str
    note: str = ""
    files: list[Path] = field(default_factory=list)


def plan_meta(root: Path, recursive: bool, fields: set[str], fmt_album: str, fmt_artist: str,
              progress=None) -> list[MetaItem]:
    todo: list[tuple[Path, str, str, list[Path]]] = []
    for d in iter_dirs(root, recursive):
        audio = [p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        if "folder" in fields and has_japanese(d.name):
            todo.append((d, "folder", d.name, []))
        for fld in ("album", "artist", "albumartist"):
            if fld not in fields:
                continue
            values: dict[str, list[Path]] = {}
            for p in audio:
                try:
                    for v in get_tag(p, fld):
                        if has_japanese(v):
                            values.setdefault(v, []).append(p)
                except Exception:
                    pass
            for v, ps in values.items():
                todo.append((d, fld, v, ps))
    items = []
    for i, (d, fld, old, ps) in enumerate(todo, 1):
        if progress:
            progress(i, len(todo), old)
        mode = fmt_artist if fld in ("artist", "albumartist") else fmt_album
        new, note = convert_text(old, mode, keep_prefix=(fld == "folder"))
        if fld == "folder":
            new = sanitize(new)
        items.append(MetaItem(d, fld, old, new, note, ps))
    return items


def apply_meta(root: Path, items: list[MetaItem], log=print) -> Path:
    """Write tags, then rename folders (deepest first, the selected folder last).
    Returns the root folder path afterwards."""
    batch = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "renames": [], "tags": []}
    # 1. tags
    for it in items:
        if it.fld == "folder" or not it.new or it.new == it.old:
            continue
        changed = 0
        for p in it.files:
            try:
                vals = get_tag(p, it.fld)
                new_vals = [it.new if v == it.old else v for v in vals]
                if new_vals != vals:
                    set_tag(p, it.fld, new_vals)
                    batch["tags"].append([_rel(root, str(p)), it.fld, vals])
                    changed += 1
            except Exception as e:
                log(f"FAILED: {it.fld} on {p.name}: {e}")
        log(f"{FIELD_LABELS[it.fld]}: {it.old}  ->  {it.new}  ({changed} file(s))")
    # 2. folders, deepest first
    folders = sorted((it for it in items if it.fld == "folder" and it.new and it.new != it.old),
                     key=lambda it: len(it.folder.parts), reverse=True)
    new_root = root
    for it in folders:
        src = it.folder
        if not src.exists():
            log(f"Skipped (missing): {src}")
            continue
        target = _unique_target(src.parent, sanitize(it.new), set(), src)
        try:
            os.rename(src, target)
        except OSError as e:
            log(f"FAILED: folder {src.name}: {e}  (is a file inside open in another program?)")
            continue
        log(f"Renamed folder: {src.name}  ->  {target.name}")
        if src == root:
            batch["root_rename"] = [src.name, target.name]
            new_root = target
        else:
            batch["renames"].append([_rel(root, str(src)), _rel(root, str(target))])
    if batch["tags"] or batch["renames"] or batch.get("root_rename"):
        data = _read_log(new_root)
        data.append(batch)
        _write_log(new_root, data)
    return new_root


# ---------------------------------------------------------------- cover art
def find_cover(folder: Path) -> Path | None:
    imgs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not imgs:
        return None
    by_stem = {p.stem.lower(): p for p in imgs}
    for n in COVER_NAMES:
        if n in by_stem:
            return by_stem[n]
    for p in imgs:
        if p.stem.lower().startswith("albumart"):
            return p
    return max(imgs, key=lambda p: p.stat().st_size)  # largest image as a fallback


@dataclass
class CoverImage:
    data: bytes
    mime: str
    width: int
    height: int
    depth: int


def load_cover(path: Path, max_px: int | None = 1500) -> CoverImage:
    from PIL import Image
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        fmt = im.format
        w, h = im.size
        need_convert = fmt not in ("JPEG", "PNG") or (max_px and max(w, h) > max_px)
        if not need_convert:
            mode_depth = {"1": 1, "L": 8, "P": 8, "RGB": 24, "RGBA": 32, "CMYK": 32}.get(im.mode, 24)
            return CoverImage(raw, "image/jpeg" if fmt == "JPEG" else "image/png", w, h, mode_depth)
        im = im.convert("RGB")
        if max_px and max(w, h) > max_px:
            im.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return CoverImage(buf.getvalue(), "image/jpeg", im.width, im.height, 24)


def _flac_picture(c: CoverImage):
    from mutagen.flac import Picture
    pic = Picture()
    pic.type = 3
    pic.mime = c.mime
    pic.desc = "Cover"
    pic.width, pic.height, pic.depth = c.width, c.height, c.depth
    pic.data = c.data
    return pic


def _apic(c: CoverImage):
    from mutagen.id3 import APIC
    return APIC(encoding=3, mime=c.mime, type=3, desc="Cover", data=c.data)


def embed_cover(path: Path, c: CoverImage, replace: bool = True) -> str:
    """Returns 'ok', 'skipped (has cover)' or 'unsupported'."""
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    from mutagen.oggopus import OggOpus
    from mutagen.wave import WAVE
    from mutagen.aiff import AIFF
    from mutagen.id3 import ID3, ID3NoHeaderError

    f = mutagen.File(path)
    if f is None:
        return "unsupported"

    if isinstance(f, FLAC):
        if f.pictures and not replace:
            return "skipped (has cover)"
        f.clear_pictures()
        f.add_picture(_flac_picture(c))
        f.save()
        return "ok"

    if isinstance(f, MP3):
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        if tags.getall("APIC") and not replace:
            return "skipped (has cover)"
        tags.delall("APIC")
        tags.add(_apic(c))
        tags.save(path, v2_version=3)
        return "ok"

    if isinstance(f, MP4):
        if f.tags is None:
            f.add_tags()
        if f.tags.get("covr") and not replace:
            return "skipped (has cover)"
        fmt = MP4Cover.FORMAT_PNG if c.mime == "image/png" else MP4Cover.FORMAT_JPEG
        f.tags["covr"] = [MP4Cover(c.data, imageformat=fmt)]
        f.save()
        return "ok"

    if isinstance(f, (OggVorbis, OggOpus)):
        if f.tags is None:
            f.add_tags()
        if f.tags.get("metadata_block_picture") and not replace:
            return "skipped (has cover)"
        for k in ("metadata_block_picture", "coverart", "coverartmime"):
            if k in f.tags:
                del f.tags[k]
        f.tags["metadata_block_picture"] = [
            base64.b64encode(_flac_picture(c).write()).decode("ascii")]
        f.save()
        return "ok"

    if isinstance(f, (WAVE, AIFF)):
        if f.tags is None:
            f.add_tags()
        if f.tags.getall("APIC") and not replace:
            return "skipped (has cover)"
        f.tags.delall("APIC")
        f.tags.add(_apic(c))
        try:
            f.save(v2_version=3)
        except TypeError:
            f.save()
        return "ok"

    return "unsupported"


def embed_folder(root: Path, recursive: bool, manual_cover: Path | None, replace: bool,
                 max_px: int | None, log=print) -> dict:
    stats = {"ok": 0, "skipped": 0, "unsupported": 0, "failed": 0, "no_cover": 0}
    cache: dict[Path, CoverImage] = {}
    for d in iter_dirs(root, recursive):
        audio = [p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        if not audio:
            continue
        cover_path = manual_cover or find_cover(d)
        if cover_path is None:
            log(f"No cover image found in {d} - skipped {len(audio)} file(s).")
            stats["no_cover"] += len(audio)
            continue
        if cover_path not in cache:
            cache[cover_path] = load_cover(cover_path, max_px)
        cov = cache[cover_path]
        log(f"Folder: {d.name}  (cover: {cover_path.name}, {cov.width}x{cov.height})")
        for p in audio:
            try:
                r = embed_cover(p, cov, replace)
            except Exception as e:
                stats["failed"] += 1
                log(f"  FAILED: {p.name}: {e}")
                continue
            key = "skipped" if r.startswith("skipped") else r
            stats[key] += 1
            log(f"  {r}: {p.name}")
    log("Done - " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
    return stats


# ---------------------------------------------------------------- GUI
import sys

if getattr(sys, "frozen", False):  # portable .exe: keep settings beside the exe
    SETTINGS_PATH = Path(sys.executable).parent / "JP Music Tool settings.json"
else:
    SETTINGS_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "JP Music Tool" / "settings.json"

PALETTE = {
    "light": {"bg": "#fafafa", "fg": "#1c1c1c", "muted": "#616161", "text_bg": "#ffffff",
              "stripe": "#f3f3f3", "warn": "#9d5d00", "ok": "#0f7b0f", "err": "#c42b1c",
              "thumb_bg": "#f0f0f0"},
    "dark": {"bg": "#1c1c1c", "fg": "#fafafa", "muted": "#a0a0a0", "text_bg": "#232323",
             "stripe": "#242424", "warn": "#fce100", "ok": "#6ccb5f", "err": "#ff99a4",
             "thumb_bg": "#2b2b2b"},
}


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _system_prefers_dark() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def _set_titlebar_dark(win, dark: bool):
    """Windows 10/11: make the native title bar follow the theme."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                           ctypes.sizeof(val)) == 0:
                break
        # nudge the window so the title bar repaints immediately
        w, h = win.winfo_width(), win.winfo_height()
        win.geometry(f"{w + 1}x{h}")
        win.after(10, lambda: win.geometry(f"{w}x{h}"))
    except Exception:
        pass


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root_win = tk.Tk()
    root_win.title("JP Music Tool")
    root_win.geometry("1100x820")
    root_win.minsize(820, 580)

    try:
        import sv_ttk
    except ImportError:
        sv_ttk = None

    settings = _load_settings()
    q: queue.Queue = queue.Queue()
    folder_var = tk.StringVar(value=settings.get("last_folder", ""))
    recursive_var = tk.BooleanVar(value=settings.get("recursive", False))
    translate_var = tk.BooleanVar(value=True)
    title_var = tk.BooleanVar(value=settings.get("set_title", True))
    replace_var = tk.BooleanVar(value=True)
    resize_var = tk.BooleanVar(value=True)
    dark_var = tk.BooleanVar(value=settings.get("theme", "dark" if _system_prefers_dark() else "light") == "dark")
    status_var = tk.StringVar(value="Ready." if settings.get("last_folder") else "Choose a folder to begin.")
    count_var = tk.StringVar(value="")
    state = {"items": [], "manual_cover": None, "thumb": None, "busy": False}

    BASE_FONT = ("Segoe UI", 10)
    style = ttk.Style()

    # ---------- theme
    def colors():
        return PALETTE["dark" if dark_var.get() else "light"]

    def apply_theme(save=True):
        mode = "dark" if dark_var.get() else "light"
        c = colors()
        if sv_ttk:
            sv_ttk.set_theme(mode)
        else:
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Subtitle.TLabel", font=BASE_FONT, foreground=c["muted"])
        style.configure("Muted.TLabel", font=("Segoe UI", 9), foreground=c["muted"])
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Treeview", rowheight=30)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("TNotebook.Tab", padding=(16, 6))
        log_txt.configure(bg=c["text_bg"], fg=c["fg"], insertbackground=c["fg"],
                          selectbackground="#0078d4", selectforeground="#ffffff")
        for tag in ("ok", "warn", "err"):
            log_txt.tag_configure(tag, foreground=c[tag])
        log_txt.tag_configure("muted", foreground=c["muted"])
        for tv in (tree, mtree):
            tv.tag_configure("odd", background=c["stripe"])
            tv.tag_configure("even", background="")
            tv.tag_configure("warn", foreground=c["warn"])
        thumb_lbl.configure(background=c["thumb_bg"], foreground=c["muted"])
        _set_titlebar_dark(root_win, mode == "dark")
        if save:
            settings["theme"] = mode
            _save_settings(settings)

    # ---------- plumbing
    def log(msg):
        q.put(("log", msg))

    def log_tag(msg: str) -> str:
        m = msg.lower()
        if "failed" in m or "error" in m:
            return "err"
        if "skipped" in m or "no cover" in m or "unsupported" in m:
            return "warn"
        if m.startswith(("renamed", "restored", "  ok", "done", "rename complete", "undo complete", "title tag",
                         "album:", "artist:", "album artist:", "changes applied")):
            return "ok"
        return ""

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    stamp = time.strftime("%H:%M:%S  ")
                    log_txt.configure(state="normal")
                    log_txt.insert("end", stamp, "muted")
                    log_txt.insert("end", payload + "\n", log_tag(payload))
                    log_txt.see("end")
                    log_txt.configure(state="disabled")
                elif kind == "status":
                    status_var.set(payload)
                elif kind == "busy":
                    if payload:
                        progress.pack(side="right", padx=(8, 0))
                        progress.start(12)
                    else:
                        progress.stop()
                        progress.pack_forget()
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        root_win.after(80, poll)

    def run_bg(fn, label):
        if state["busy"]:
            messagebox.showinfo("Busy", "Please wait for the current task to finish.")
            return
        state["busy"] = True
        status_var.set(label + "…")
        q.put(("busy", True))

        def wrapper():
            try:
                fn()
            except Exception as e:
                log(f"ERROR: {e}")
            finally:
                state["busy"] = False
                q.put(("busy", False))
                q.put(("status", "Ready."))
        threading.Thread(target=wrapper, daemon=True).start()

    def get_folder() -> Path | None:
        raw = folder_var.get().strip().strip('"')
        p = Path(raw)
        if not raw or not p.is_dir():
            messagebox.showwarning("Folder", "Please choose a valid folder.")
            return None
        settings["last_folder"] = str(p)
        settings["recursive"] = recursive_var.get()
        _save_settings(settings)
        return p

    # ---------- header
    outer = ttk.Frame(root_win, padding=(20, 16, 20, 12))
    outer.pack(fill="both", expand=True)

    header = ttk.Frame(outer)
    header.pack(fill="x")
    titles = ttk.Frame(header)
    titles.pack(side="left")
    ttk.Label(titles, text="JP Music Tool", style="Title.TLabel").pack(anchor="w")
    ttk.Label(titles, text="Translate Japanese file names, albums, artists and folders  ·  Embed cover art",
              style="Subtitle.TLabel").pack(anchor="w")
    switch_style = "Switch.TCheckbutton" if sv_ttk else "TCheckbutton"
    dark_sw = ttk.Checkbutton(header, text="Dark mode", style=switch_style, variable=dark_var,
                              command=apply_theme)
    dark_sw.pack(side="right", anchor="n", pady=6)

    # ---------- folder card
    card_style = "Card.TFrame" if sv_ttk else "TFrame"
    fcard = ttk.Frame(outer, style=card_style, padding=(16, 12))
    fcard.pack(fill="x", pady=(14, 12))
    ttk.Label(fcard, text="Music folder", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Entry(fcard, textvariable=folder_var, font=BASE_FONT).grid(row=1, column=0, sticky="ew", pady=(6, 0))

    def browse():
        d = filedialog.askdirectory(initialdir=folder_var.get() or None)
        if d:
            folder_var.set(os.path.normpath(d))
    ttk.Button(fcard, text="Browse…", command=browse).grid(row=1, column=1, padx=(8, 0), pady=(6, 0))
    ttk.Checkbutton(fcard, text="Include subfolders", style=switch_style,
                    variable=recursive_var).grid(row=1, column=2, padx=(16, 0), pady=(6, 0))
    fcard.columnconfigure(0, weight=1)

    paned = ttk.PanedWindow(outer, orient="vertical")
    paned.pack(fill="both", expand=True)
    nb = ttk.Notebook(paned)
    paned.add(nb, weight=4)

    # ---------- rename tab
    rt = ttk.Frame(nb, padding=12)
    nb.add(rt, text="Rename")
    bar = ttk.Frame(rt)
    bar.pack(fill="x", pady=(0, 10))
    tbl = ttk.Frame(rt)
    tbl.pack(fill="both", expand=True)
    cols = ("folder", "original", "new", "note")
    tree = ttk.Treeview(tbl, columns=cols, show="headings", selectmode="extended")
    for c, w, t, st in [("folder", 90, "Folder", False), ("original", 280, "Original name", True),
                        ("new", 420, "New name  (double-click to edit)", True), ("note", 160, "Note", False)]:
        tree.heading(c, text=t, anchor="w")
        tree.column(c, width=w, minwidth=60, anchor="w", stretch=st)
    ysb = ttk.Scrollbar(tbl, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ysb.set)
    tree.pack(side="left", fill="both", expand=True)
    ysb.pack(side="right", fill="y")

    empty_lbl = ttk.Label(tbl, text="Click “Generate preview” to list files with Japanese names.",
                          style="Muted.TLabel")
    empty_lbl.place(relx=0.5, rely=0.5, anchor="center")

    def fill_tree(base: Path):
        tree.delete(*tree.get_children())
        for i, it in enumerate(state["items"]):
            rel = os.path.relpath(it.src.parent, base)
            tags = ["odd" if i % 2 else "even"] + (["warn"] if it.note else [])
            tree.insert("", "end", iid=str(i), tags=tags,
                        values=("." if rel == "." else rel, it.src.name, it.new_name, it.note))
        n = len(state["items"])
        count_var.set(f"{n} file{'s' if n != 1 else ''}")
        if n:
            empty_lbl.place_forget()
        else:
            empty_lbl.configure(text="No files with Japanese names found.")
            empty_lbl.place(relx=0.5, rely=0.5, anchor="center")
        status_var.set(f"{n} file(s) with Japanese names. Review, then click Apply rename.")

    def clear_tree(msg="Click “Generate preview” to list files with Japanese names."):
        tree.delete(*tree.get_children())
        count_var.set("")
        empty_lbl.configure(text=msg)
        empty_lbl.place(relx=0.5, rely=0.5, anchor="center")

    def preview():
        base = get_folder()
        if not base:
            return

        def work():
            def prog(i, n, name):
                q.put(("status", f"Translating {i}/{n}: {name}"))
            items = plan_renames(base, recursive_var.get(), prog, translate_var.get())
            state["items"] = items
            q.put(("call", lambda: fill_tree(base)))
            log(f"Preview ready: {len(items)} file(s).")
        run_bg(work, "Building preview")

    def apply():
        base = get_folder()
        if not base or not state["items"]:
            messagebox.showinfo("Rename", "Generate a preview first.")
            return
        sel = tree.selection()
        idx = [int(s) for s in sel] if sel else list(range(len(state["items"])))
        if not messagebox.askyesno("Rename", f"Rename {len(idx)} file(s)?"
                                             f"{' (selected rows only)' if sel else ''}"):
            return
        chosen = [state["items"][i] for i in idx]

        def work():
            done = apply_renames(base, chosen, log, set_title=title_var.get())
            log(f"Rename complete: {len(done)} file(s). Undo log saved to {LOG_NAME}.")
            state["items"] = []
            q.put(("call", lambda: clear_tree("Done. Generate a new preview to rename more files.")))
        run_bg(work, "Renaming")

    def undo():
        base = get_folder()
        if not base or not messagebox.askyesno(
                "Undo", "Undo the most recent change in this folder?\n"
                        "(file renames, album/artist tag edits or folder renames - whichever was last)"):
            return

        def work():
            new_root = undo_last(base, log)
            state["items"], state["meta"] = [], []
            if new_root != base:
                q.put(("call", lambda: folder_var.set(str(new_root))))
            q.put(("call", lambda: (clear_tree(), clear_mtree())))
        run_bg(work, "Undoing")

    ttk.Button(bar, text="Generate preview", command=preview).pack(side="left")
    ttk.Button(bar, text="Apply rename", style="Accent.TButton", command=apply).pack(side="left", padx=8)
    ttk.Button(bar, text="Undo last change", command=undo).pack(side="left")
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=14)
    ttk.Checkbutton(bar, text="Translate online", style=switch_style,
                    variable=translate_var).pack(side="left")
    ttk.Checkbutton(bar, text="Also set Title tag", style=switch_style, variable=title_var,
                    command=lambda: (settings.__setitem__("set_title", title_var.get()), _save_settings(settings))
                    ).pack(side="left", padx=(14, 0))
    ttk.Label(bar, textvariable=count_var, style="Muted.TLabel").pack(side="right")
    ttk.Label(rt, text="Tip: select rows to rename only those. “Also set Title tag” writes the new name (minus track number) into each file's Title.",
              style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    def edit_cell(event):
        row = tree.identify_row(event.y)
        if not row or tree.identify_column(event.x) != "#3":
            return
        x, y, w, h = tree.bbox(row, "#3")
        e = ttk.Entry(tree, font=BASE_FONT)
        e.insert(0, tree.set(row, "new"))
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()
        e.select_range(0, "end")

        def save(_=None):
            val = e.get().strip()
            if val:
                it = state["items"][int(row)]
                if not val.lower().endswith(it.src.suffix.lower()):
                    val += it.src.suffix
                it.new_name = val
                tree.set(row, "new", val)
            e.destroy()
        e.bind("<Return>", save)
        e.bind("<FocusOut>", save)
        e.bind("<Escape>", lambda _: e.destroy())
    tree.bind("<Double-1>", edit_cell)

    # ---------- album / artist / folder tab
    mt = ttk.Frame(nb, padding=12)
    nb.add(mt, text="Album · Artist · Folder")
    meta_vars = {k: tk.BooleanVar(value=True) for k in ("album", "artist", "albumartist", "folder")}
    fmt_album_var = tk.StringVar(value=settings.get("fmt_album", "English (Romaji)"))
    fmt_artist_var = tk.StringVar(value=settings.get("fmt_artist", "Romaji only"))
    meta_count = tk.StringVar(value="")

    mrow1 = ttk.Frame(mt)
    mrow1.pack(fill="x")
    ttk.Label(mrow1, text="Change", style="Section.TLabel").pack(side="left", padx=(0, 12))
    for k in ("album", "artist", "albumartist", "folder"):
        ttk.Checkbutton(mrow1, text=FIELD_LABELS[k], style=switch_style,
                        variable=meta_vars[k]).pack(side="left", padx=(0, 14))

    mrow2 = ttk.Frame(mt)
    mrow2.pack(fill="x", pady=(10, 0))
    ttk.Label(mrow2, text="Album & folder format").pack(side="left")
    ttk.Combobox(mrow2, textvariable=fmt_album_var, values=list(FORMATS), state="readonly",
                 width=18).pack(side="left", padx=(8, 20))
    ttk.Label(mrow2, text="Artist format").pack(side="left")
    ttk.Combobox(mrow2, textvariable=fmt_artist_var, values=list(FORMATS), state="readonly",
                 width=18).pack(side="left", padx=(8, 0))

    mbar = ttk.Frame(mt)
    mbar.pack(fill="x", pady=(10, 10))
    mtbl = ttk.Frame(mt)
    mtbl.pack(fill="both", expand=True)
    mcols = ("folder", "field", "current", "new", "files")
    mtree = ttk.Treeview(mtbl, columns=mcols, show="headings", selectmode="extended")
    for c, w, t, st in [("folder", 140, "Folder", False), ("field", 100, "What", False),
                        ("current", 260, "Current", True), ("new", 360, "New  (double-click to edit)", True),
                        ("files", 60, "Files", False)]:
        mtree.heading(c, text=t, anchor="w")
        mtree.column(c, width=w, minwidth=50, anchor="w", stretch=st)
    msb = ttk.Scrollbar(mtbl, orient="vertical", command=mtree.yview)
    mtree.configure(yscrollcommand=msb.set)
    mtree.pack(side="left", fill="both", expand=True)
    msb.pack(side="right", fill="y")
    MT_EMPTY = "Pick what to change, then click “Generate preview”."
    mempty = ttk.Label(mtbl, text=MT_EMPTY, style="Muted.TLabel")
    mempty.place(relx=0.5, rely=0.5, anchor="center")
    state["meta"] = []

    def fill_mtree(base: Path):
        mtree.delete(*mtree.get_children())
        for i, it in enumerate(state["meta"]):
            rel = os.path.relpath(it.folder, base)
            tags = ["odd" if i % 2 else "even"] + (["warn"] if it.note else [])
            mtree.insert("", "end", iid=str(i), tags=tags,
                         values=("." if rel == "." else rel, FIELD_LABELS[it.fld], it.old, it.new,
                                 "" if it.fld == "folder" else len(it.files)))
        n = len(state["meta"])
        meta_count.set(f"{n} change{'s' if n != 1 else ''}")
        if n:
            mempty.place_forget()
        else:
            mempty.configure(text="Nothing in Japanese found for the selected options.")
            mempty.place(relx=0.5, rely=0.5, anchor="center")
        status_var.set(f"{n} change(s) found. Review, then click Apply changes.")

    def clear_mtree(msg=MT_EMPTY):
        mtree.delete(*mtree.get_children())
        meta_count.set("")
        mempty.configure(text=msg)
        mempty.place(relx=0.5, rely=0.5, anchor="center")

    def preview_meta():
        base = get_folder()
        if not base:
            return
        fields = {k for k, v in meta_vars.items() if v.get()}
        if not fields:
            messagebox.showinfo("Nothing selected", "Turn on at least one of Album, Artist, Album artist or Folder name.")
            return
        settings["fmt_album"], settings["fmt_artist"] = fmt_album_var.get(), fmt_artist_var.get()
        _save_settings(settings)

        def work():
            def prog(i, n, name):
                q.put(("status", f"Converting {i}/{n}: {name}"))
            state["meta"] = plan_meta(base, recursive_var.get(), fields, FORMATS[fmt_album_var.get()],
                                      FORMATS[fmt_artist_var.get()], prog)
            q.put(("call", lambda: fill_mtree(base)))
            log(f"Preview ready: {len(state['meta'])} album/artist/folder change(s).")
        run_bg(work, "Building preview")

    def apply_meta_gui():
        base = get_folder()
        if not base or not state["meta"]:
            messagebox.showinfo("Apply", "Generate a preview first.")
            return
        sel = mtree.selection()
        idx = [int(s) for s in sel] if sel else list(range(len(state["meta"])))
        chosen = [state["meta"][i] for i in idx]
        n_fold = sum(1 for it in chosen if it.fld == "folder")
        n_tag = len(chosen) - n_fold
        msg = f"Update {n_tag} tag value(s) and rename {n_fold} folder(s)?"
        if sel:
            msg += "\n(selected rows only)"
        if n_fold:
            msg += "\n\nClose any music player or Explorer window using these folders first."
        if not messagebox.askyesno("Apply changes", msg):
            return

        def work():
            new_root = apply_meta(base, chosen, log)
            log(f"Changes applied. Undo log saved to {LOG_NAME}.")
            state["meta"], state["items"] = [], []   # paths may have changed
            if new_root != base:
                q.put(("call", lambda: folder_var.set(str(new_root))))
            q.put(("call", lambda: (clear_mtree("Done. Generate a new preview to make more changes."),
                                    clear_tree())))
        run_bg(work, "Applying changes")

    ttk.Button(mbar, text="Generate preview", command=preview_meta).pack(side="left")
    ttk.Button(mbar, text="Apply changes", style="Accent.TButton", command=apply_meta_gui).pack(side="left", padx=8)
    ttk.Button(mbar, text="Undo last change", command=undo).pack(side="left")
    ttk.Label(mbar, textvariable=meta_count, style="Muted.TLabel").pack(side="right")
    ttk.Label(mt, text="Album, Artist and Album artist are tags inside the audio files. "
                       "Folder name renames the chosen folder (and subfolders if included).",
              style="Muted.TLabel").pack(side="bottom", anchor="w", pady=(8, 0), before=mtbl)

    def edit_meta(event):
        row = mtree.identify_row(event.y)
        if not row or mtree.identify_column(event.x) != "#4":
            return
        x, y, w, h = mtree.bbox(row, "#4")
        e = ttk.Entry(mtree, font=BASE_FONT)
        e.insert(0, mtree.set(row, "new"))
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()
        e.select_range(0, "end")

        def save(_=None):
            val = e.get().strip()
            if val:
                it = state["meta"][int(row)]
                it.new = sanitize(val) if it.fld == "folder" else val
                mtree.set(row, "new", it.new)
            e.destroy()
        e.bind("<Return>", save)
        e.bind("<FocusOut>", save)
        e.bind("<Escape>", lambda _: e.destroy())
    mtree.bind("<Double-1>", edit_meta)

    # ---------- cover tab
    ct = ttk.Frame(nb, padding=16)
    nb.add(ct, text="Cover art")
    tcard = ttk.Frame(ct, style=card_style, padding=10)
    tcard.grid(row=0, column=0, sticky="n", padx=(0, 20))
    thumb_lbl = tk.Label(tcard, text="No cover", width=30, height=13, font=BASE_FONT, bd=0)
    thumb_lbl.pack()
    thumb_cap = ttk.Label(tcard, text="", style="Muted.TLabel")
    thumb_cap.pack(pady=(8, 0))

    side = ttk.Frame(ct)
    side.grid(row=0, column=1, sticky="nsew")
    ttk.Label(side, text="Cover image", style="Section.TLabel").pack(anchor="w")
    cover_info = tk.StringVar(value="Choose a folder.")
    ttk.Label(side, textvariable=cover_info, wraplength=520, justify="left").pack(anchor="w", pady=(4, 10))
    btns = ttk.Frame(side)
    btns.pack(anchor="w")

    ttk.Label(side, text="Options", style="Section.TLabel").pack(anchor="w", pady=(14, 2))
    ttk.Checkbutton(side, text="Replace existing cover art", style=switch_style,
                    variable=replace_var).pack(anchor="w", pady=3)
    ttk.Checkbutton(side, text="Shrink images larger than 1500 px", style=switch_style,
                    variable=resize_var).pack(anchor="w", pady=3)
    embed_btn = ttk.Button(side, text="Embed cover in all files", style="Accent.TButton")
    embed_btn.pack(anchor="w", pady=(14, 6))
    ttk.Label(side, text="Supports FLAC, MP3, M4A/MP4, OGG, Opus, WAV and AIFF.\n"
                         "Audio is never re-encoded; only the tags change.",
              style="Muted.TLabel", justify="left").pack(anchor="w")
    ct.columnconfigure(1, weight=1)

    def show_thumb(path: Path | None):
        if path is None:
            thumb_lbl.configure(image="", text="No cover found", width=30, height=13)
            thumb_cap.configure(text="")
            state["thumb"] = None
            return
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as im:
                size = f"{im.width} × {im.height}"
                im = im.convert("RGB")
                im.thumbnail((240, 240))
                state["thumb"] = ImageTk.PhotoImage(im)
            thumb_lbl.configure(image=state["thumb"], text="", width=240, height=240)
            thumb_cap.configure(text=f"{path.name}  ·  {size}")
        except Exception as e:
            thumb_lbl.configure(image="", text=f"Cannot preview:\n{e}", width=30, height=13)

    def refresh_cover(*_):
        p = Path(folder_var.get().strip().strip('"'))
        if state["manual_cover"]:
            cover_info.set(f"Using your chosen image for every folder:\n{state['manual_cover']}")
            show_thumb(state["manual_cover"])
        elif folder_var.get().strip() and p.is_dir():
            c = find_cover(p)
            cover_info.set(f"Auto-detected from the folder (subfolders use their own cover):\n{c}" if c else
                           "No image found in this folder. Click “Choose image…”.")
            show_thumb(c)
        else:
            cover_info.set("Choose a folder.")
            show_thumb(None)

    def choose_img():
        f = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp")])
        if f:
            state["manual_cover"] = Path(f)
            refresh_cover()

    def clear_img():
        state["manual_cover"] = None
        refresh_cover()

    def embed():
        base = get_folder()
        if not base:
            return
        if not messagebox.askyesno("Embed cover", "Embed the cover into all audio files in this folder"
                                   f"{' and its subfolders' if recursive_var.get() else ''}?"):
            return
        run_bg(lambda: embed_folder(base, recursive_var.get(), state["manual_cover"], replace_var.get(),
                                    1500 if resize_var.get() else None, log), "Embedding cover")

    ttk.Button(btns, text="Choose image…", command=choose_img).pack(side="left")
    ttk.Button(btns, text="Use auto-detected", command=clear_img).pack(side="left", padx=8)
    ttk.Button(btns, text="Refresh", command=refresh_cover).pack(side="left")
    embed_btn.configure(command=embed)
    folder_var.trace_add("write", refresh_cover)

    # ---------- activity log
    lf = ttk.Frame(paned, padding=(0, 10, 0, 0))
    paned.add(lf, weight=1)
    lhead = ttk.Frame(lf)
    lhead.pack(fill="x", pady=(0, 6))
    ttk.Label(lhead, text="Activity", style="Section.TLabel").pack(side="left")

    def clear_log():
        log_txt.configure(state="normal")
        log_txt.delete("1.0", "end")
        log_txt.configure(state="disabled")
    ttk.Button(lhead, text="Clear", command=clear_log).pack(side="right")
    lbody = ttk.Frame(lf, style=card_style, padding=2)
    lbody.pack(fill="both", expand=True)
    log_txt = tk.Text(lbody, height=5, wrap="word", font=("Cascadia Mono", 9), bd=0,
                      highlightthickness=0, padx=10, pady=8, state="disabled")
    lsb = ttk.Scrollbar(lbody, orient="vertical", command=log_txt.yview)
    log_txt.configure(yscrollcommand=lsb.set)
    log_txt.pack(side="left", fill="both", expand=True)
    lsb.pack(side="right", fill="y")

    # ---------- status bar
    sbar = ttk.Frame(outer)
    sbar.pack(fill="x", side="bottom", pady=(10, 0), before=paned)
    ttk.Label(sbar, textvariable=status_var, style="Muted.TLabel").pack(side="left")
    progress = ttk.Progressbar(sbar, mode="indeterminate", length=160)

    apply_theme(save=False)
    refresh_cover()
    if not sv_ttk:
        dark_sw.state(["disabled"])
        log("Dark mode needs the sv-ttk package:  pip install sv-ttk")
    root_win.after(50, lambda: _set_titlebar_dark(root_win, dark_var.get()))
    poll()
    root_win.mainloop()


def _selftest(out_file: str):
    """Hidden check for the portable build:  "JP Music Tool.exe" --selftest result.txt"""
    lines = []
    for s in ["夜に駆ける", "ハルジオン", "だから僕は音楽を辞めた"]:
        try:
            lines.append(f"{s} -> {build_name(s, '.flac')}")
        except Exception as e:
            lines.append(f"{s} -> ERROR {e!r}")
    lines.append(f"cutlet loaded: {_cutlet() is not None}")
    try:
        import sv_ttk  # noqa: F401
        from PIL import ImageTk  # noqa: F401
        lines.append("sv_ttk + ImageTk: ok")
    except Exception as e:
        lines.append(f"imports: ERROR {e!r}")
    Path(out_file).write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        _selftest(sys.argv[2])
    else:
        run_gui()
