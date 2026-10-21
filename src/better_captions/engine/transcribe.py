from __future__ import annotations

import importlib
import itertools
import re
from typing import Callable

from better_captions.schema import Word

ProgressFn = Callable[[float], None]  # fraction 0..1


def transcribe(
    audio_path: str,
    model: str = "large-v3-turbo",
    language: str | None = None,
    device: str = "auto",
    progress: ProgressFn | None = None,
    backend: str = "auto",
) -> tuple[str, float, list[Word]]:
    """Return (language, duration_seconds, words) with word-level timestamps; Word.speaker is None.

    backend: "faster-whisper", "mlx" (Apple Silicon), "whispercpp" (Vulkan: AMD/Intel/NVIDIA GPUs) or "auto".
    Every backend module exposes transcribe() with this signature minus ``backend``, and available() -> bool.
    """
    name = choose_backend(backend)
    if name != "faster-whisper":
        module = importlib.import_module(f"better_captions.engine.backends.{BACKENDS[name]}")
        return module.transcribe(audio_path, model=model, language=language, device=device, progress=progress)
    whisper = _load_model(model, device)
    try:
        segments, info = _start(whisper, audio_path, language)
    except RuntimeError as exc:
        # cuBLAS/cuDNN are only loaded on first use, so "auto" can pick CUDA and fail here: redo it on the CPU.
        if device != "auto" or resolve_device(device)[0] == "cpu" or not _CUDA_ERROR.search(str(exc)):
            raise
        segments, info = _start(_load_model(model, "cpu"), audio_path, language)
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


# CTranslate2's errors for a missing/broken CUDA runtime, e.g. "Library libcublas.so.12 is not found or cannot be loaded".
_CUDA_ERROR = re.compile(r"cuda|cublas|cudnn|nvrtc|library", re.IGNORECASE)


def _start(whisper, audio_path: str, language: str | None):
    """Start decoding and return (segments, info); the first segment is decoded now so CUDA errors surface here."""
    # Segments are a generator: decoding happens while we iterate, so progress is live.
    segments, info = whisper.transcribe(audio_path, language=language, word_timestamps=True, vad_filter=True)
    first = next(segments, None)
    return itertools.chain([] if first is None else [first], segments), info


# backend name -> module under better_captions.engine.backends (faster-whisper lives in this module)
BACKENDS = {"faster-whisper": None, "mlx": "mlx", "whispercpp": "whispercpp"}


def choose_backend(backend: str = "auto") -> str:
    """Resolve "auto" to the best backend available on this machine."""
    if backend not in BACKENDS and backend != "auto":
        raise ValueError(f"unknown backend {backend!r}; expected auto or one of {', '.join(BACKENDS)}")
    if backend != "auto":
        return backend
    # A CUDA GPU makes faster-whisper the best choice; otherwise prefer a GPU-capable alternative.
    if _cuda_available():
        return "faster-whisper"
    for name in AUTO_ORDER:
        if _backend_available(name):
            return name
    return "faster-whisper"


# Alternatives tried by "auto" when there is no CUDA GPU, in order of preference.
AUTO_ORDER = ["mlx", "whispercpp"]


def _backend_available(name: str) -> bool:
    """Ask the backend module's available() (cheap: no model loading); missing modules count as unavailable."""
    try:
        module = importlib.import_module(f"better_captions.engine.backends.{BACKENDS[name]}")
        return bool(module.available())
    except Exception:
        return False


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
    if device != "cpu":
        # Before ctranslate2 needs them: make the `cuda` extra's cuBLAS/cuDNN loadable (no-op without it).
        from better_captions.engine import gpu

        gpu.setup()
    from faster_whisper import WhisperModel

    name, index, compute_type = resolve_device(device)
    try:
        return WhisperModel(model, device=name, device_index=index, compute_type=compute_type)
    except Exception:
        # "auto" picked CUDA but its runtime (cuBLAS/cuDNN) is unusable: fall back to the CPU.
        if device != "auto" or name == "cpu":
            raise
        return WhisperModel(model, device="cpu", compute_type="int8")
