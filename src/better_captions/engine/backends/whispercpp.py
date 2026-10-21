"""whisper.cpp (Vulkan) backend: AMD, Intel and NVIDIA GPUs on Windows and Linux.

Runs the ``whisper-cli`` executable from the Lemonade SDK team's whisper.cpp-rocm release
(a whisper.cpp fork that publishes prebuilt Vulkan binaries; MIT), downloaded on first use
into ``<data dir>/bin``, with ggml models from Hugging Face in the models cache.
Word timestamps come from the ``-ojf`` full-JSON token output (DTW alignment when the
binary and model support it). See docs/GPU_VULKAN.md.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from better_captions.paths import data_dir, models_dir
from better_captions.schema import Word

ProgressFn = Callable[[float], None]

# --- pinned binary -------------------------------------------------------------

RELEASE = "v1.8.4"  # Lemonade whisper.cpp-rocm release == upstream whisper.cpp v1.8.4
RELEASE_URL = f"https://github.com/lemonade-sdk/whisper.cpp-rocm/releases/download/{RELEASE}"


@dataclass(frozen=True)
class Asset:
    name: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{RELEASE_URL}/{self.name}"


# (sys.platform, normalised machine) -> release archive. SHA256s verified against the archives
# themselves and the GitHub release digests (see docs/GPU_VULKAN.md).
ASSETS = {
    ("win32", "x86_64"): Asset(
        "whisper-v1.8.4-windows-vulkan-x64.zip", "e0d20a0f92e31b98adc0faf71172efc810b701e6391a9d858ca045bff26f77cd"
    ),
    ("linux", "x86_64"): Asset(
        "whisper-v1.8.4-linux-vulkan-x86_64.tar.gz", "55d0bdf9be7092ed8148b27d26353f5755446381eb9e1e5b6b00d5c761d813b1"
    ),
}
BINARY_NAME = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"

# --- models --------------------------------------------------------------------

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
MODEL_ALIASES = {"large": "large-v3", "turbo": "large-v3-turbo"}
# ggml conversions published in ggerganov/whisper.cpp (models/download-ggml-model.sh).
KNOWN_MODELS = (
    "tiny", "tiny.en", "tiny-q5_1", "tiny.en-q5_1", "tiny-q8_0",
    "base", "base.en", "base-q5_1", "base.en-q5_1", "base-q8_0",
    "small", "small.en", "small-q5_1", "small.en-q5_1", "small-q8_0",
    "medium", "medium.en", "medium-q5_0", "medium.en-q5_0", "medium-q8_0",
    "large-v1", "large-v2", "large-v2-q5_0", "large-v2-q8_0",
    "large-v3", "large-v3-q5_0", "large-v3-turbo", "large-v3-turbo-q5_0", "large-v3-turbo-q8_0",
)  # fmt: skip
# whisper.cpp ``-dtw`` alignment-head presets; quantised variants use their base model's preset.
DTW_PRESETS = {
    "tiny": "tiny", "tiny.en": "tiny.en", "base": "base", "base.en": "base.en",
    "small": "small", "small.en": "small.en", "medium": "medium", "medium.en": "medium.en",
    "large-v1": "large.v1", "large-v2": "large.v2", "large-v3": "large.v3", "large-v3-turbo": "large.v3.turbo",
}  # fmt: skip
_QUANT_SUFFIX = re.compile(r"-q\d_\d$")

# Languages written without spaces: every token is a word (same list faster-whisper uses).
NO_SPACE_LANGUAGES = {"zh", "ja", "th", "lo", "my"}
_SPECIAL_TOKEN = re.compile(r"^\[_[A-Z0-9_]+\]$")  # [_BEG_], [_TT_397], [_SOT_], ...
# whisper.cpp non-speech annotations: [BLANK_AUDIO], [MUSIC], (music), (applause), *laughs*
_NON_SPEECH = re.compile(r"^(?:\[[^\]]*\]|\([^()]*\)|\*[^*]*\*)$")
_PROGRESS = re.compile(r"progress\s*=\s*(\d+)%")

VK_PHYSICAL_DEVICE_TYPE_CPU = 4  # software rasterisers such as llvmpipe/lavapipe


# --- public contract -----------------------------------------------------------


def available() -> bool:
    """True when this platform has a prebuilt binary and a Vulkan GPU is present. Cheap; never downloads."""
    forced = os.environ.get("BETTER_CAPTIONS_VULKAN")
    if forced is not None:
        return forced.strip().lower() not in ("", "0", "false", "no", "off")
    if platform_asset() is None:
        return False
    try:
        return any(kind != VK_PHYSICAL_DEVICE_TYPE_CPU for _, kind in _vulkan_devices())
    except Exception:
        return False


def transcribe(
    audio_path: str,
    model: str = "large-v3-turbo",
    language: str | None = None,
    device: str = "auto",
    progress: ProgressFn | None = None,
) -> tuple[str, float, list[Word]]:
    """Return (language, duration_seconds, words) like engine.transcribe.transcribe()."""
    binary = ensure_binary()
    model_path = ensure_model(model)
    dtw = dtw_preset(model) if _dtw_enabled() and supports_dtw(binary) else None
    with tempfile.TemporaryDirectory(prefix="better-captions-") as tmp:
        out_base = Path(tmp) / "out"
        cmd = build_command(binary, model_path, audio_path, language, device, out_base, dtw)
        reported = _run(cmd, _environment(binary), progress)
        data = json.loads((out_base.with_name("out.json")).read_text(encoding="utf-8"))
    detected, words, last_end = parse_result(data)
    duration = audio_duration(audio_path)
    if duration is None:
        duration = last_end
    if progress and reported < 1.0:
        progress(1.0)
    return detected, duration, words


# --- binary ------------------------------------------------------------------------


def platform_asset() -> Asset | None:
    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        machine = "x86_64"
    return ASSETS.get((sys.platform, machine))


def bin_dir() -> Path:
    return data_dir() / "bin" / f"whispercpp-{RELEASE}"


def binary_path() -> Path | None:
    """The installed whisper-cli, or None (the Linux tarball nests it in a top-level folder)."""
    return _find_binary(bin_dir())


def _find_binary(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    return next((p for p in sorted(root.rglob(BINARY_NAME)) if p.is_file()), None)


def ensure_binary() -> Path:
    """Path to whisper-cli, downloading and verifying the pinned release archive on first use."""
    found = binary_path()
    if found is not None:
        return found
    asset = platform_asset()
    if asset is None:
        raise RuntimeError(
            f"the whispercpp backend has no prebuilt Vulkan binary for {sys.platform}/{platform.machine()}; "
            "see docs/GPU_VULKAN.md"
        )
    root = bin_dir()
    root.parent.mkdir(parents=True, exist_ok=True)
    # Download and extract into a sibling staging directory, renamed into place only once that has
    # succeeded: an interrupted install never leaves a partial bin_dir() for binary_path() to find.
    staging = Path(tempfile.mkdtemp(prefix=f"{root.name}.staging-", dir=root.parent))
    try:
        archive = staging / asset.name
        _download(asset.url, archive, sha256=asset.sha256)
        _extract(archive, staging)
        archive.unlink()
        staged = _find_binary(staging)
        if staged is None:
            raise RuntimeError(f"{BINARY_NAME} not found in {asset.name} (extracted to {root})")
        if sys.platform != "win32":
            staged.chmod(staged.stat().st_mode | 0o111)
        try:
            if root.exists() and binary_path() is None:  # left behind by an older, interrupted install
                shutil.rmtree(root)
            os.replace(staging, root)
        except OSError:
            # A concurrent install finished first and its copy is complete: use that one.
            found = binary_path()
            if found is None:
                raise
            return found
        return root / staged.relative_to(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _extract(archive: Path, dest: Path) -> None:
    """Extract the release archive into dest, refusing members that would land outside it."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if _inside(dest, info.filename):
                    zf.extract(info, dest)
        return
    with tarfile.open(archive, "r:*") as tar:
        members = [m for m in tar.getmembers() if (m.isfile() or m.isdir() or m.issym()) and _inside(dest, m.name)]
        members = [
            m for m in members if not m.issym() or _inside(dest, os.path.join(os.path.dirname(m.name), m.linkname))
        ]
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # Python without PEP 706 filters: members are already vetted above
            tar.extractall(dest, members=members)


def _inside(dest: Path, name: str) -> bool:
    if not name or os.path.isabs(name) or name.startswith(("/", "\\")):
        return False
    target = os.path.realpath(os.path.join(dest, name))
    return target == os.path.realpath(dest) or target.startswith(os.path.realpath(dest) + os.sep)


_help_cache: dict[str, bool] = {}


def supports_dtw(binary: Path) -> bool:
    """Whether this whisper-cli accepts ``-dtw`` (checked once per binary via ``--help``)."""
    key = str(binary)
    if key not in _help_cache:
        try:
            proc = subprocess.run(
                [key, "--help"], capture_output=True, text=True, timeout=30, env=_environment(binary), **_popen_flags()
            )
            _help_cache[key] = "--dtw" in (proc.stdout + proc.stderr)
        except Exception:
            _help_cache[key] = False
    return _help_cache[key]


def _dtw_enabled() -> bool:
    """BETTER_CAPTIONS_WHISPERCPP_DTW=0 turns DTW (and with it -nfa) off, for speed."""
    return os.environ.get("BETTER_CAPTIONS_WHISPERCPP_DTW", "1").strip().lower() not in ("0", "false", "no", "off")


def _environment(binary: Path) -> dict[str, str]:
    """The Linux build's RUNPATH points at its CI build tree: load libwhisper/libggml from beside the binary."""
    env = dict(os.environ)
    if sys.platform != "win32":
        current = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = str(binary.parent) + (os.pathsep + current if current else "")
    return env


def _popen_flags() -> dict:
    # Don't flash a console window when launched from the Resolve plugin on Windows.
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}


# --- models -------------------------------------------------------------------------


def ggml_model_name(model: str) -> str:
    """Our model name (faster-whisper style, e.g. large-v3-turbo) -> ggml file name."""
    name = MODEL_ALIASES.get(model, model)
    if name not in KNOWN_MODELS:
        raise ValueError(
            f"unknown whisper.cpp model {model!r}; expected one of {', '.join(KNOWN_MODELS)} or a path to a ggml .bin"
        )
    return f"ggml-{name}.bin"


def dtw_preset(model: str) -> str | None:
    """whisper.cpp ``-dtw`` preset for the model, or None when it has none (or is a file path)."""
    name = MODEL_ALIASES.get(model, model)
    return DTW_PRESETS.get(_QUANT_SUFFIX.sub("", name))


def ensure_model(model: str) -> Path:
    """Path to the ggml model, downloading it into the models cache on first use. A .bin path is used as is."""
    if model.endswith(".bin") and Path(model).is_file():
        return Path(model)
    dest = models_dir() / "whispercpp" / ggml_model_name(model)
    if not dest.exists():
        _download(f"{MODEL_URL}/{dest.name}", dest)
    return dest


# --- running whisper-cli ---------------------------------------------------------------


def device_args(device: str) -> list[str]:
    """ "cpu" -> no GPU; "auto"/"vulkan"/"gpu"/"cuda" (optionally ":N") -> Vulkan device N."""
    name, _, index = device.partition(":")
    if name == "cpu":
        return ["-ng"]
    if name in ("auto", "gpu", "vulkan", "cuda"):
        return ["-dev", str(int(index))] if index else []
    raise ValueError(f"unknown device {device!r} for the whispercpp backend; expected auto, cpu, vulkan or vulkan:N")


def build_command(
    binary: Path, model_path: Path, audio_path: str, language: str | None, device: str, out_base: Path, dtw: str | None
) -> list[str]:
    threads = max(1, min(os.cpu_count() or 1, 8))
    cmd = [str(binary), "-m", str(model_path), "-f", str(audio_path), "-l", language or "auto"]
    # -ojf: full JSON with per-token timestamps; -pp: progress on stderr; -np: no banner.
    cmd += ["-ojf", "-of", str(out_base), "-pp", "-np", "-t", str(threads)]
    if dtw:
        # DTW alignment-head timestamps are only computed with flash attention off.
        cmd += ["-dtw", dtw, "-nfa"]
    cmd += device_args(device)
    return cmd


def parse_progress_line(line: str) -> float | None:
    """'whisper_print_progress_callback: progress =  5%' -> 0.05; None for other lines."""
    m = _PROGRESS.search(line)
    return min(int(m.group(1)) / 100.0, 1.0) if m else None


def _run(cmd: list[str], env: dict[str, str], progress: ProgressFn | None) -> float:
    """Run whisper-cli, forwarding -pp progress lines (monotonic); return the last fraction reported.

    Raises RuntimeError with the tail of stderr when whisper-cli exits non-zero.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **_popen_flags(),
    )
    tail: list[str] = []
    last = 0.0
    assert proc.stderr is not None
    for line in proc.stderr:
        tail.append(line)
        del tail[:-40]
        frac = parse_progress_line(line)
        if progress and frac is not None and frac > last:
            last = frac
            progress(frac)
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"whisper-cli failed (exit {code}): {''.join(tail).strip()[-2000:]}")
    return last


# --- JSON -> words ---------------------------------------------------------------------


def parse_result(data: dict) -> tuple[str, list[Word], float]:
    """Full-JSON output -> (language, words, end of the last segment in seconds)."""
    language = (data.get("result") or {}).get("language") or (data.get("params") or {}).get("language") or "en"
    split_every_token = language in NO_SPACE_LANGUAGES
    words: list[Word] = []
    last_end = 0.0
    for segment in data.get("transcription") or []:
        words.extend(merge_tokens(segment.get("tokens") or [], split_every_token=split_every_token))
        last_end = max(last_end, float((segment.get("offsets") or {}).get("to", 0)) / 1000.0)
    return language, words, last_end


def merge_tokens(tokens: list[dict], split_every_token: bool = False) -> list[Word]:
    """Merge one segment's sub-word tokens into words.

    A token starting with whitespace begins a new word; others (word pieces, punctuation) join the
    previous one. Special tokens ([_BEG_], [_TT_n]) and non-speech annotations ([BLANK_AUDIO], (music),
    *laughs*) are dropped. Times are the token ``offsets``
    (ms); when every token of a word carries a DTW time (``t_dtw``, centiseconds, roughly where the
    token ends) the word ends there and starts where the previous word ended.
    """
    groups: list[list[dict]] = []
    for tok in tokens:
        text = tok.get("text", "")
        if not text or _SPECIAL_TOKEN.match(text) or not text.strip():
            continue
        if groups and not split_every_token and not text[0].isspace():
            groups[-1].append(tok)
        else:
            groups.append([tok])

    words: list[Word] = []
    prev_dtw_end: float | None = None
    for group in groups:
        text = "".join(t["text"] for t in group).strip()
        if _NON_SPEECH.match(text):
            continue
        offsets = [t.get("offsets") for t in group if t.get("offsets")]
        start = float(offsets[0]["from"]) / 1000.0 if offsets else (words[-1].end if words else 0.0)
        end = float(offsets[-1]["to"]) / 1000.0 if offsets else start
        dtw = [float(t.get("t_dtw", -1)) for t in group]
        if dtw and all(t >= 0 for t in dtw):
            end = max(dtw) / 100.0
            if prev_dtw_end is not None:
                # Start where the previous word ended, but not before whisper's own start estimate
                # (keeps a long pause from being glued to the following word).
                start = max(prev_dtw_end, min(start, end))
            start = min(start, end)
            prev_dtw_end = end
        else:
            prev_dtw_end = None
        probability = sum(float(t.get("p", 1.0)) for t in group) / len(group)
        words.append(Word(text=text, start=start, end=max(end, start), probability=probability))
    return words


def audio_duration(path: str) -> float | None:
    try:
        import soundfile as sf

        return float(sf.info(path).duration)
    except Exception:
        return None


# --- download ---------------------------------------------------------------------------


def _download(url: str, dest: Path, sha256: str | None = None) -> None:
    """Download url to dest via a .part file; verify sha256 when given (mismatch removes the file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"could not download {url} to {dest}: {e} (download it there by hand to work offline)"
        ) from e
    if sha256 is not None and digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: expected sha256 {sha256}, got {digest.hexdigest()}")
    os.replace(tmp, dest)


# --- Vulkan probe -------------------------------------------------------------------------


class _VkApplicationInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_int),
        ("pNext", ctypes.c_void_p),
        ("pApplicationName", ctypes.c_char_p),
        ("applicationVersion", ctypes.c_uint32),
        ("pEngineName", ctypes.c_char_p),
        ("engineVersion", ctypes.c_uint32),
        ("apiVersion", ctypes.c_uint32),
    ]


class _VkInstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ("sType", ctypes.c_int),
        ("pNext", ctypes.c_void_p),
        ("flags", ctypes.c_uint32),
        ("pApplicationInfo", ctypes.POINTER(_VkApplicationInfo)),
        ("enabledLayerCount", ctypes.c_uint32),
        ("ppEnabledLayerNames", ctypes.c_void_p),
        ("enabledExtensionCount", ctypes.c_uint32),
        ("ppEnabledExtensionNames", ctypes.c_void_p),
    ]


def _vulkan_devices() -> list[tuple[str, int]]:
    """(device name, VkPhysicalDeviceType) of every device the system Vulkan loader exposes.

    Talks to the loader (vulkan-1.dll / libvulkan.so.1, installed by GPU drivers) through ctypes,
    so no SDK or vulkaninfo is needed. Raises when there is no loader or no usable instance.
    """
    names = ["vulkan-1.dll"] if sys.platform == "win32" else ["libvulkan.so.1", "libvulkan.so"]
    lib = None
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        raise OSError("no Vulkan loader")
    lib.vkCreateInstance.restype = ctypes.c_int
    lib.vkEnumeratePhysicalDevices.restype = ctypes.c_int
    lib.vkGetPhysicalDeviceProperties.restype = None
    lib.vkDestroyInstance.restype = None
    app = _VkApplicationInfo(sType=0, apiVersion=(1 << 22) | (1 << 12))  # VK_API_VERSION_1_1
    info = _VkInstanceCreateInfo(sType=1, pApplicationInfo=ctypes.pointer(app))
    instance = ctypes.c_void_p()
    if lib.vkCreateInstance(ctypes.byref(info), None, ctypes.byref(instance)) != 0:
        raise OSError("vkCreateInstance failed")
    try:
        count = ctypes.c_uint32(0)
        if lib.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None) != 0:
            raise OSError("vkEnumeratePhysicalDevices failed")
        handles = (ctypes.c_void_p * max(count.value, 1))()
        lib.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), handles)
        devices = []
        for i in range(count.value):
            # VkPhysicalDeviceProperties: apiVersion, driverVersion, vendorID, deviceID (u32 each),
            # deviceType (enum) at byte 16, deviceName[256] at byte 20; the rest we don't need.
            props = ctypes.create_string_buffer(4096)
            lib.vkGetPhysicalDeviceProperties(ctypes.c_void_p(handles[i]), props)
            kind = ctypes.cast(props, ctypes.POINTER(ctypes.c_uint32))[4]
            name = props.raw[20 : 20 + 256].split(b"\0", 1)[0].decode("utf-8", errors="replace")
            devices.append((name, int(kind)))
        return devices
    finally:
        lib.vkDestroyInstance(instance, None)
