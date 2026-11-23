"""Local archive (NAS) for raw media and full transcripts.

The archive is deliberately a *separate root* from the data dir: `publish`
globs `data_dir/*.parquet`, so anything written here is never published.
Raw audio, captions and full transcripts of third-party recordings (TV
debates, politicians' channels) stay on the local network; only metadata
and derived tables go to the public lake.

Root: $CARAMELO_ARCHIVE_DIR (e.g. an NFS/SMB mount of the NAS), else
./archive.

Layout:
    <root>/youtube/<channel_id>/<video_id>/   info.json, *.vtt, audio.*
    <root>/instagram/<username>/<shortcode>/  info.json, audio.*
    <root>/debates/<debate_id>/<n>/           info.json, *.vtt, audio.*
    <root>/web/<outlet>/<yyyymmdd>.html.gz    raw front pages (Wayback)
    <root>/transcricoes.parquet               compiled transcript segments

Each item dir may hold transcript.<method>.json — the per-item unit of
work that `transcribe` writes and `compile_transcripts` folds into one
Parquet. Per-item files keep runs resumable and NAS-friendly (no single
big file rewritten on every item).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SEGMENT_SCHEMA = pa.schema([
    ("source_kind", pa.string()),   # youtube | instagram | debate
    ("source_id", pa.string()),     # video_id | <debate_id>/<n>
    ("seg_idx", pa.int32()),
    ("start_s", pa.float64()),
    ("end_s", pa.float64()),
    ("text", pa.string()),
    ("speaker", pa.string()),       # diarization label; null until v2
    ("method", pa.string()),        # yt_manual | yt_auto | whisper:<model>
    ("lang", pa.string()),
])

# Preference when an item has several transcripts: human captions beat
# Whisper, Whisper beats YouTube's auto-captions.
METHOD_RANK = {"yt_manual": 0, "whisper": 1, "yt_auto": 2}


def _atomic(path: Path, write) -> None:
    """Write via a sibling temp file + rename. On the SMB/NFS share a reader
    in another process can otherwise see a half-flushed file."""
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


def root(override: Path | None = None) -> Path:
    r = override or Path(os.environ.get("CARAMELO_ARCHIVE_DIR", "archive"))
    r.mkdir(parents=True, exist_ok=True)
    return r


def youtube_dir(archive: Path, channel_id: str, video_id: str) -> Path:
    return archive / "youtube" / channel_id / video_id


def platform_dir(archive: Path, platform: str, account: str,
                 item_id: str) -> Path:
    return archive / platform / account / item_id


def debate_dir(archive: Path, debate_id: str, n: int) -> Path:
    return archive / "debates" / debate_id / str(n)


# --- yt-dlp ---------------------------------------------------------------

# Original-language Portuguese tracks only. "pt.*" would also match every
# auto-translated variant (pt-PT, ...), one request each -> HTTP 429.
SUB_LANGS = "pt-orig,pt,pt-BR"


def _ytdlp_base() -> list[str]:
    """Prefer the project's yt-dlp (`.[transcribe]`: pinned, with curl-cffi
    browser impersonation, which YouTube increasingly requires); fall back
    to a system binary."""
    import importlib.util
    if importlib.util.find_spec("yt_dlp"):
        exe = [sys.executable, "-m", "yt_dlp"]
    else:
        found = shutil.which("yt-dlp")
        if not found:
            raise RuntimeError("yt-dlp not found: uv pip install -e "
                               "'.[transcribe]'") from None
        exe = [found]
    cmd = exe + ["--no-progress", "--no-playlist",
                 # polite pacing: YouTube throttles bursty clients
                 "--sleep-requests", "1", "--sleep-interval", "2",
                 "--max-sleep-interval", "6", "--sleep-subtitles", "3"]
    cookies = os.environ.get("CARAMELO_YTDLP_COOKIES")
    if cookies:
        cmd += ["--cookies", cookies]
    return cmd


INFO_KEYS = ("id", "title", "fulltitle", "description", "channel",
             "channel_id", "uploader", "uploader_id", "upload_date",
             "timestamp", "duration", "view_count", "like_count",
             "comment_count", "tags", "categories", "language", "was_live",
             "live_status", "webpage_url", "extractor", "chapters")


def trim_info(path: Path) -> None:
    """yt-dlp's info.json is 0.5-2MB, almost all stream formats and signed
    URLs that expire anyway. Keep the descriptive fields and only the
    *names* of caption tracks (caption_method needs to know which are
    human-made)."""
    info = json.loads(path.read_text())
    slim = {k: info[k] for k in INFO_KEYS if k in info}
    slim["subtitles"] = {k: [] for k in info.get("subtitles") or {}}
    slim["automatic_captions"] = {
        k: [] for k in info.get("automatic_captions") or {}
        if k.startswith("pt")}
    body = json.dumps(slim, ensure_ascii=False)
    _atomic(path, lambda p: p.write_text(body))


def fetch_media(url: str, dest: Path, *, audio: bool) -> dict:
    """Captions (manual + auto, Portuguese) + info.json, and optionally the
    best audio-only stream (no re-encode). Idempotent: skips what exists.
    Returns {"captions": [...], "audio": path|None, "ok": bool, "error"}."""
    dest.mkdir(parents=True, exist_ok=True)
    have_info = (dest / "media.info.json").exists()
    have_audio = any(dest.glob("audio.*"))
    result = {"captions": [], "audio": None, "ok": True, "error": None}

    if not have_info:
        # a failed caption track must not lose the info.json: write the
        # metadata first, captions second (their failure is non-fatal and
        # retried next run, since captions_pass only needs the .vtt)
        proc = subprocess.run(
            _ytdlp_base() + ["--skip-download", "--write-info-json",
                             "-o", str(dest / "media.%(ext)s"), url],
            capture_output=True, text=True)
        if proc.returncode != 0:
            result.update(ok=False, error=proc.stderr.strip()[-500:])
            return result
        trim_info(dest / "media.info.json")
    no_caps = dest / ".no_captions"
    if not any(dest.glob("media.*.vtt")) and not no_caps.exists():
        # by URL, not --load-info-json: caption URLs are signed and expire,
        # and the stored info.json is trimmed anyway
        proc = subprocess.run(
            _ytdlp_base() + [
                "--skip-download", "--write-subs", "--write-auto-subs",
                "--sub-langs", SUB_LANGS, "--sub-format", "vtt",
                "-o", str(dest / "media.%(ext)s"), url],
            capture_output=True, text=True)
        if proc.returncode != 0:
            result["error"] = "captions: " + proc.stderr.strip()[-300:]
        elif not any(dest.glob("media.*.vtt")):
            no_caps.touch()  # no Portuguese track: don't ask again

    if audio and not have_audio:
        proc = subprocess.run(
            _ytdlp_base() + [
                "-f", "bestaudio[ext=m4a]/bestaudio",
                "-o", str(dest / "audio.%(ext)s"), url],
            capture_output=True, text=True)
        if proc.returncode != 0:
            result.update(ok=False, error=proc.stderr.strip()[-500:])

    result["captions"] = sorted(str(p) for p in dest.glob("media.*.vtt"))
    result["audio"] = next((str(p) for p in dest.glob("audio.*")
                            if not p.name.endswith(".part")), None)
    return result


def caption_method(vtt: Path, info: dict | None) -> str:
    """yt-dlp names both manual and auto subs media.<lang>.vtt; the
    info.json tells which language keys were human-made."""
    lang = vtt.name.split(".")[-2]
    manual = set((info or {}).get("subtitles") or {})
    return "yt_manual" if lang in manual else "yt_auto"


# --- compile ----------------------------------------------------------------

def item_dirs(archive: Path):
    """-> (source_kind, source_id, dir) for every archived item."""
    for platform in ("youtube", "instagram"):
        for d in sorted((archive / platform).glob("*/*")):
            if d.is_dir():
                yield platform, d.name, d
    for d in sorted((archive / "debates").glob("*/*")):
        if d.is_dir():
            yield "debate", f"{d.parent.name}/{d.name}", d


def write_transcript(d: Path, method: str, lang: str | None,
                     segments: list[dict]) -> Path:
    key = method.replace(":", "-").replace("/", "-")
    out = d / f"transcript.{key}.json"
    body = json.dumps({"method": method, "lang": lang, "segments": segments},
                      ensure_ascii=False)
    _atomic(out, lambda p: p.write_text(body))
    return out


def compile_transcripts(archive: Path, *, best_only: bool = True) -> Path:
    """Fold every item's transcript.*.json into <root>/transcricoes.parquet.
    best_only keeps one transcript per item (manual > whisper > auto)."""
    rows: list[dict] = []
    items = 0
    for kind, source_id, d in item_dirs(archive):
        docs = [json.loads(p.read_text()) for p in d.glob("transcript.*.json")]
        if not docs:
            continue
        items += 1
        docs.sort(key=lambda t: METHOD_RANK.get(t["method"].split(":")[0], 9))
        for doc in docs[:1] if best_only else docs:
            for i, s in enumerate(doc["segments"]):
                rows.append({
                    "source_kind": kind, "source_id": source_id,
                    "seg_idx": i, "start_s": s["start"], "end_s": s["end"],
                    "text": s["text"], "speaker": s.get("speaker"),
                    "method": doc["method"], "lang": doc.get("lang"),
                })
    out = archive / "transcricoes.parquet"
    table = pa.Table.from_pylist(rows, schema=SEGMENT_SCHEMA)
    _atomic(out, lambda p: pq.write_table(table, p, compression="zstd"))
    print(f"archive: {items} transcribed items, {len(rows)} segments -> {out}")
    return out
