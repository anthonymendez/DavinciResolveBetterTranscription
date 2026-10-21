# NVIDIA GPU support

faster-whisper runs on CTranslate2, which uses an NVIDIA GPU only when it can load the
**CUDA 12 cuBLAS** and **cuDNN 9** runtime libraries. The NVIDIA driver does not ship them,
and the `ctranslate2` wheels don't bundle them. Without them CTranslate2 still counts
the GPU (`get_cuda_device_count()` only needs the driver). The model even loads on `cuda`.
It then fails on first use with `RuntimeError: Library libcublas.so.12 is not found or cannot
be loaded` (Windows: `cublas64_12.dll`).

## Decision: the `nvidia-*-cu12` pip wheels, on Windows and Linux

The engine has a `cuda` optional extra (`pyproject.toml`):

```toml
cuda = [
    "nvidia-cublas-cu12>=12.4,<13; sys_platform == 'win32' or sys_platform == 'linux'",
    "nvidia-cudnn-cu12>=9.1,<10; sys_platform == 'win32' or sys_platform == 'linux'",
]
```

faster-whisper's README documents these wheels for Linux only. **They also ship Windows
DLLs.** PyPI has `win_amd64` wheels for both. As of 2026-10, uv.lock resolves
`nvidia-cublas-cu12` 12.9.2.10, `nvidia-cudnn-cu12` 9.27.0.42, and
`nvidia-cuda-nvrtc-cu12` 12.9.86, which cuBLAS depends on. Each has `win_amd64`,
`manylinux_2_27_x86_64` and `aarch64` wheels. Listing the wheels shows where the
libraries go:

| Wheel | Windows (`site-packages/…`) | Linux (`site-packages/…`) |
|---|---|---|
| cuBLAS | `nvidia/cublas/bin/cublas64_12.dll`, `cublasLt64_12.dll` | `nvidia/cublas/lib/libcublas.so.12`, `libcublasLt.so.12` |
| cuDNN | `nvidia/cudnn/bin/cudnn64_9.dll`, `cudnn_*64_9.dll` (graph, ops, cnn, adv, engines_*, heuristic, ext) | `nvidia/cudnn/lib/libcudnn.so.9`, `libcudnn_*.so.9` |
| NVRTC | `nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll`, `nvrtc-builtins64_129.dll` | `nvidia/cuda_nvrtc/lib/libnvrtc.so.12`, `libnvrtc-builtins.so.12.9` |

The wheels also contain `nvblas` and `*.alt.*` builds. Those are never preloaded: nvblas
overrides BLAS symbols, and the `.alt` libraries duplicate the main ones.

Why not the alternatives:

- **Purfview's [whisper-standalone-win](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)
  `libs` archives.** These are cuBLAS 12.4/12.8 + cuDNN 9.5/9.8 in a single archive, and
  faster-whisper's README points Windows users to them. But they are a third-party
  repackaging. The installer would need custom download, verification and unpack code for
  them, and they lag NVIDIA's releases. The pip wheels come from NVIDIA through PyPI, are
  hash-pinned in `uv.lock`, and install with the same `uv pip install` as everything else.
- **A system CUDA toolkit or Docker image.** Too heavy, needs admin rights, and is not
  "out of the box". It still works: if the libraries are already on the system search
  path, the engine uses them.

Cost: about 1.4 GB of downloads (cuDNN 730 MiB, cuBLAS 554 MiB, NVRTC 85 MiB). That is
why the installer adds the extra only when it finds an NVIDIA GPU.

## Installer

`install.ps1` (Windows) and `install.sh` (Linux) decide before installing the engine:

- `BETTER_CAPTIONS_GPU=auto` (default): install the extra if an NVIDIA GPU is present.
  The check is `nvidia-smi -L`, which is installed with the NVIDIA driver. On Windows it
  falls back to a `Win32_VideoController` whose name or vendor contains "NVIDIA".
- `BETTER_CAPTIONS_GPU=cuda`: always install it. `none`: never. Any other value is an
  error.
- macOS has no CUDA, so the answer there is always `none`.

The installer then runs `uv pip install "<source>[cuda]"` and records `"gpu": "cuda"` or
`"none"` in `config.json`. Re-running it keeps the runtime: nothing is uninstalled when a
later run picks `none`.

## Engine: making the libraries loadable (`engine/gpu.py`)

The system loader never looks in `site-packages/nvidia/*`. CTranslate2 opens cuBLAS and
cuDNN by bare name (`dlopen("libcublas.so.12")`, `LoadLibrary("cublas64_12.dll")`) the first
time a model runs on CUDA. So `transcribe._load_model` calls `gpu.setup()` first, except
for `--device cpu`. `setup()` is idempotent, never raises, and does nothing without the
extra. It works as follows:

- **Windows:** adds each `nvidia/<pkg>/bin` with `os.add_dll_directory` and prepends it to
  `PATH`. A plain `LoadLibrary` call ignores `add_dll_directory` but searches `PATH`.
- **Windows and Linux:** preloads the cuBLAS, NVRTC and cuDNN libraries with
  `ctypes.CDLL`, using `RTLD_GLOBAL` on Linux. A later load by the same name gets the
  already-loaded copy.
- Libraries that fail to load are retried while others still succeed, because their
  dependency order isn't known. Libraries that never load are skipped.

**Why not `LD_LIBRARY_PATH` on Linux:** faster-whisper's README says to set it before
starting Python. The dynamic loader reads it once at process start, so changing
`os.environ` in-process has no effect. Re-executing the CLI with it set would also work,
but it adds a process and complicates the plugin's subprocess handling. Preloading does
the same job in-process.

**CPU fallback:** with `--device auto` the engine picks CUDA whenever CTranslate2 sees a
GPU. If the CUDA model fails to load, or fails on first use with a CUDA/cuBLAS/cuDNN
library error, `transcribe()` reloads the model on the CPU (`int8`). It does not fall back
for an explicit `--device cuda`.

Checked on Linux (WSL2, RTX 5090, ctranslate2 4.8.3) with the wheel versions above:

- Without `gpu.setup()`, `WhisperModel("tiny", device="cuda")` fails with
  `Library libcublas.so.12 is not found`.
- With it, all 14 libraries preload and transcription runs on the GPU.
- With setup disabled, `device="auto"` falls back to the CPU.

## Checking a machine

```
<data dir>/venv/bin/python -m better_captions.cli doctor            # Linux/macOS
& "$env:LOCALAPPDATA\BetterCaptions\venv\Scripts\python.exe" -m better_captions.cli doctor   # Windows
```

This prints JSON:

- `nvidia_gpus`: what nvidia-smi reports.
- `cuda_runtime`: the library dirs registered and the libraries loaded.
- `cuda`: the ctranslate2 version, CUDA device count and compute types.
- `backends`: which backends are available.
- `backend` and `device`: what `--backend auto --device auto` would use.

## Sources

- faster-whisper README, "GPU" requirements: cuBLAS for CUDA 12 and cuDNN 9; pip install
  on Linux only, with `LD_LIBRARY_PATH`; Purfview's libs for Windows.
  <https://github.com/SYSTRAN/faster-whisper#gpu>
- PyPI `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`, release files including `win_amd64`:
  <https://pypi.org/project/nvidia-cublas-cu12/#files>,
  <https://pypi.org/project/nvidia-cudnn-cu12/#files>
- ONNX Runtime's `preload_dlls()` loads the same wheels from `nvidia/cublas/bin` and
  `nvidia/cudnn/bin` on Windows.
  <https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>
- CTranslate2's Windows package init only adds its own (and ROCm) DLL dirs, nothing for
  CUDA. <https://github.com/OpenNMT/CTranslate2/blob/master/python/ctranslate2/__init__.py>
- Purfview whisper-standalone-win `libs` release:
  <https://github.com/Purfview/whisper-standalone-win/releases/tag/libs>
- Python `os.add_dll_directory`, which affects dependency resolution for `ctypes` and
  extension modules but not plain `LoadLibrary`:
  <https://docs.python.org/3/library/os.html#os.add_dll_directory>
