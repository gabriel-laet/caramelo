"""Turn archived media into transcript segments (local, GPU).

Two passes over the archive, both resumable per item:

1. captions — parse YouTube .vtt (manual or auto) into segments. Free and
   instant; auto-captions are decent for recent videos, poor for old
   broadcasts.
2. whisper  — faster-whisper on the archived audio, for items with audio
   and no human captions (all debates, by default). Optional dependency:
   `uv pip install -e '.[transcribe]'`.

Then compile_transcripts() folds everything into <archive>/transcricoes.parquet.
Speaker diarization (who is talking) is a v2 step that will fill the
`speaker` column; v1 is raw text with timestamps.
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from caramelo import archive as arc

_TS = r"(\d+):(\d{2}):(\d{2})[.,](\d{3})"
_CUE = re.compile(_TS + r"\s*-->\s*" + _TS)
_TAG = re.compile(r"<[^>]+>")


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list[dict]:
    """WebVTT -> [{start, end, text}].

    YouTube auto-captions are "rolling": each cue repeats the previous line
    and appends a new one, with 10ms transition cues in between. Keep only
    the last line of each cue, drop near-zero cues, and collapse repeats, so
    the concatenated text reads once.
    """
    segs: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start, end = _secs(*m.groups()[:4]), _secs(*m.groups()[4:])
        i += 1
        body = []
        # cue bodies end at a truly empty line; YouTube's auto-subs put a
        # lone-space line inside cues, so .strip() would end them early
        while i < len(lines) and lines[i].rstrip("\r") != "":
            # ">>" (escaped &gt;&gt;) marks a speaker change: keep it
            body.append(html.unescape(_TAG.sub("", lines[i])).strip())
            i += 1
        body = [b for b in body if b]
        if not body or end - start < 0.05:
            continue
        line = body[-1]
        if segs and line == segs[-1]["text"]:
            segs[-1]["end"] = end
            continue
        segs.append({"start": start, "end": end, "text": line})
    return segs


def captions_pass(archive_root: Path) -> int:
    n = 0
    for _kind, _sid, d in arc.item_dirs(archive_root):
        vtts = sorted(d.glob("media.*.vtt"))
        if not vtts:
            continue
        info_path = d / "media.info.json"
        info = json.loads(info_path.read_text()) if info_path.exists() else None
        # one transcript per method; prefer pt-BR / pt over translated tracks
        by_method: dict[str, Path] = {}
        for v in vtts:
            lang = v.name.split(".")[-2]
            if not lang.startswith("pt"):
                continue
            method = arc.caption_method(v, info)
            if method not in by_method or lang in ("pt-BR", "pt", "pt-orig"):
                by_method[method] = v
        for method, v in by_method.items():
            if (d / f"transcript.{method}.json").exists():
                continue
            segs = parse_vtt(v.read_text(errors="replace"))
            if segs:
                arc.write_transcript(d, method, v.name.split(".")[-2], segs)
                n += 1
    print(f"transcribe: {n} caption transcripts written")
    return n


def _preload_cuda_libs() -> None:
    """CTranslate2 dlopens cuBLAS/cuDNN by soname. When they come from
    NVIDIA's pip wheels (the `transcribe` extra) instead of a system CUDA
    install, they aren't on the loader path — load them globally first so
    those dlopens resolve without LD_LIBRARY_PATH."""
    import ctypes
    import importlib

    libs: list[Path] = []
    for mod in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        try:
            libs += sorted(Path(next(iter(
                importlib.import_module(mod).__path__))).glob("lib*.so*"))
        except (ImportError, StopIteration):
            continue
    for _ in range(2):  # second pass picks up order-dependent leftovers
        pending = []
        for so in libs:
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pending.append(so)
        libs = pending


def whisper_pass(archive_root: Path, *, model: str = "large-v3",
                 kinds: tuple[str, ...] = ("debate", "youtube", "instagram"),
                 limit: int = 50, skip_if_manual: bool = True) -> int:
    _preload_cuda_libs()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit("faster-whisper not installed: "
                         "uv pip install -e '.[transcribe]'") from exc

    method = f"whisper:{model}"
    todo = []
    for kind, sid, d in arc.item_dirs(archive_root):
        if kind not in kinds:
            continue
        audio = next((p for p in d.glob("audio.*")
                      if not p.name.endswith(".part")), None)
        if not audio or (d / f"transcript.whisper-{model}.json").exists():
            continue
        if skip_if_manual and (d / "transcript.yt_manual.json").exists():
            continue
        todo.append((sid, d, audio))
    todo = todo[:limit]
    if not todo:
        print("transcribe: nothing for whisper")
        return 0

    device = os.environ.get("CARAMELO_WHISPER_DEVICE", "cuda")
    compute = os.environ.get("CARAMELO_WHISPER_COMPUTE",
                             "float16" if device == "cuda" else "int8")
    wm = WhisperModel(model, device=device, compute_type=compute)
    for i, (sid, d, audio) in enumerate(todo, 1):
        segments, info = wm.transcribe(
            str(audio), language="pt", vad_filter=True, beam_size=5,
            condition_on_previous_text=False)  # less hallucination drift
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
                 "text": s.text.strip()} for s in segments if s.text.strip()]
        arc.write_transcript(d, method, info.language, segs)
        print(f"transcribe: [{i}/{len(todo)}] {sid}: {len(segs)} segments "
              f"({info.duration / 60:.0f} min)")
    return len(todo)


def run(archive_root: Path, *, whisper: bool, model: str, limit: int,
        kinds: tuple[str, ...], data_dir: Path | None = None) -> None:
    captions_pass(archive_root)
    if whisper:
        whisper_pass(archive_root, model=model, kinds=kinds, limit=limit)
    out = arc.compile_transcripts(archive_root)
    if data_dir is not None:
        # transcript text is public (published to the lake); raw audio,
        # captions files and info.json stay in the archive
        import shutil
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / "transcricoes.parquet"
        shutil.copyfile(out, dest.with_suffix(".parquet.tmp"))
        os.replace(dest.with_suffix(".parquet.tmp"), dest)
        print(f"transcribe: published copy -> {dest}")
