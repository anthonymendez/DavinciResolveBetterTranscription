"""NVIDIA GPU support: detect GPUs and make the pip-installed CUDA runtime loadable by CTranslate2.

The `cuda` extra (nvidia-cublas-cu12, nvidia-cudnn-cu12, and nvidia-cuda-nvrtc-cu12 pulled in by
cuBLAS) installs its libraries in site-packages/nvidia/<package>/bin on Windows and .../lib on Linux,
where the system loader never looks. CTranslate2 opens cuBLAS/cuDNN by bare name the first time a
model runs on CUDA, so before that, setup():
- on Windows adds those dirs with os.add_dll_directory and to PATH (a plain LoadLibrary searches PATH);
- on both preloads the libraries with ctypes, so a later dlopen/LoadLibrary of the same name reuses
  the loaded copy. On Linux this stands in for LD_LIBRARY_PATH, which is only read at process start.
See docs/GPU.md.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# nvidia/<package> -> name fragments of the libraries to preload. Not nvblas: it overrides BLAS symbols.
RUNTIME_PACKAGES = {"cublas": ("cublas",), "cuda_nvrtc": ("nvrtc",), "cudnn": ("cudnn",)}

_setup_result: dict | None = None
_keep_alive: list = []  # DLL directory handles and loaded libraries, held for the process lifetime


def setup() -> dict:
    """Make the `cuda` extra's libraries loadable, once per process; never raises.

    Returns {"dirs": [...], "loaded": [...]} (both empty without the extra), plus "error" on failure.
    """
    global _setup_result
    if _setup_result is None:
        try:
            _setup_result = register(library_dirs(nvidia_roots()))
        except Exception as exc:  # GPU setup must not break the CPU path
            _setup_result = {"dirs": [], "loaded": [], "error": f"{type(exc).__name__}: {exc}"}
    return _setup_result


def nvidia_roots() -> list[Path]:
    """The site-packages/nvidia dirs (a namespace package, so there may be several)."""
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    return [Path(p) for p in spec.submodule_search_locations]


def library_dirs(roots: list[Path], platform: str = sys.platform) -> list[Path]:
    """The existing runtime library dirs under `roots`: nvidia/<package>/bin on Windows, /lib elsewhere."""
    sub = "bin" if platform == "win32" else "lib"
    return [root / pkg / sub for root in roots for pkg in RUNTIME_PACKAGES if (root / pkg / sub).is_dir()]


def runtime_libraries(dirs: list[Path], platform: str = sys.platform) -> list[Path]:
    """The cuBLAS/cuDNN/NVRTC libraries in `dirs` (cublas64_12.dll, libcudnn.so.9, ...)."""
    libraries = []
    for d in dirs:
        fragments = RUNTIME_PACKAGES.get(d.parent.name, ())
        for f in sorted(d.iterdir()):
            is_library = f.name.endswith(".dll") if platform == "win32" else ".so" in f.name
            # *.alt.* are alternative builds of the same library; loading both would clash.
            if is_library and ".alt." not in f.name and any(frag in f.name for frag in fragments):
                libraries.append(f)
    return libraries


def register(dirs: list[Path], platform: str = sys.platform) -> dict:
    """Add `dirs` to the DLL search path (Windows) and preload their runtime libraries."""
    if platform == "win32":
        for d in dirs:
            _keep_alive.append(os.add_dll_directory(str(d)))
        entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
        new = [str(d) for d in dirs if str(d) not in entries]
        if new:
            os.environ["PATH"] = os.pathsep.join(new + entries)
    loaded = _preload(runtime_libraries(dirs, platform), platform)
    return {"dirs": [str(d) for d in dirs], "loaded": [p.name for p in loaded]}


def _preload(libraries: list[Path], platform: str) -> list[Path]:
    """Load every library, retrying failures while others still succeed (dependency order is unknown).

    Libraries that never load (e.g. a missing optional dependency) are skipped: CTranslate2 reports
    a clear error if it really needs one, and transcribe() then falls back to the CPU.
    """
    mode = 0 if platform == "win32" else ctypes.RTLD_GLOBAL
    loaded: list[Path] = []
    pending = list(libraries)
    while pending:
        failed = []
        for lib in pending:
            try:
                _keep_alive.append(_load_library(str(lib), mode))
                loaded.append(lib)
            except OSError:
                failed.append(lib)
        if len(failed) == len(pending):
            break
        pending = failed
    return loaded


def _load_library(path: str, mode: int):
    return ctypes.CDLL(path, mode=mode)


NVIDIA_SMI_QUERY = ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"]


def nvidia_gpus(run=subprocess.run) -> list[dict]:
    """The NVIDIA GPUs nvidia-smi (installed with the driver) reports; [] without a driver."""
    try:
        result = run(NVIDIA_SMI_QUERY, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi(result.stdout)


def parse_nvidia_smi(text: str) -> list[dict]:
    """Parse `nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.rsplit(",", 2)]
        if len(parts) != 3 or not parts[0]:
            continue
        name, driver, memory = parts
        gpus.append({"name": name, "driver": driver, "memory_mb": int(memory) if memory.isdigit() else None})
    return gpus


def cuda_info() -> dict:
    """What CTranslate2 (faster-whisper's inference library) sees: version, CUDA devices, compute types."""
    try:
        import ctranslate2
    except Exception as exc:
        return {"ctranslate2": None, "cuda_devices": 0, "error": f"{type(exc).__name__}: {exc}"}
    info: dict = {"ctranslate2": getattr(ctranslate2, "__version__", None), "cuda_devices": 0}
    try:
        info["cuda_devices"] = int(ctranslate2.get_cuda_device_count())
        if info["cuda_devices"]:
            info["cuda_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
