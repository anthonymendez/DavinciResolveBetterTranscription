from __future__ import annotations

from typing import Callable

from better_captions.schema import Word

ProgressFn = Callable[[float], None]  # fraction 0..1


def transcribe(
    audio_path: str,
    model: str = "large-v3-turbo",
    language: str | None = None,
    device: str = "auto",
    progress: ProgressFn | None = None,
) -> tuple[str, float, list[Word]]:
    """Return (language, duration_seconds, words) with word-level timestamps; Word.speaker is None."""
    whisper = _load_model(model, device)
    # Segments are a generator: decoding happens while we iterate, so progress is live.
    segments, info = whisper.transcribe(audio_path, language=language, word_timestamps=True, vad_filter=True)
    duration = float(info.duration)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(text=text, start=float(w.start), end=float(w.end), probability=float(w.probability)))
        if progress and duration > 0:
            progress(min(float(seg.end) / duration, 1.0))
    return info.language, duration, words


def resolve_device(device: str = "auto") -> tuple[str, int, str]:
    """Map a device spec ("auto", "cpu", "cuda", "cuda:1") to (device, device_index, compute_type)."""
    name, _, index = device.partition(":")
    if name == "auto":
        name = "cuda" if _cuda_available() else "cpu"
    if name not in ("cpu", "cuda"):
        raise ValueError(f"unknown device {device!r}; expected auto, cpu, cuda or cuda:N")
    return name, int(index or 0), "float16" if name == "cuda" else "int8"


def _cuda_available() -> bool:
    try:
        import ctranslate2  # faster-whisper's inference backend

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _load_model(model: str, device: str):
    from faster_whisper import WhisperModel

    name, index, compute_type = resolve_device(device)
    try:
        return WhisperModel(model, device=name, device_index=index, compute_type=compute_type)
    except Exception:
        # "auto" picked CUDA but its runtime (cuBLAS/cuDNN) is unusable: fall back to the CPU.
        if device != "auto" or name == "cpu":
            raise
        return WhisperModel(model, device="cpu", compute_type="int8")
