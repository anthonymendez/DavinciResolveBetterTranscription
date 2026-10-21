"""NVIDIA GPU support with everything faked (CI has no GPU): detection, DLL/so registration,
the transcribe hooks and CPU fallback, `better-captions doctor`, and the installers' GPU decision."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from better_captions import cli
from better_captions.engine import gpu
from better_captions.engine import transcribe as tr

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install"

# --------------------------------------------------------------------------- detection


def test_parse_nvidia_smi():
    text = "NVIDIA GeForce RTX 4090, 560.94, 24564\n\nNVIDIA RTX A6000, 550.54.15, [N/A]\nTesla, Inc. GPU, 1.0, 8\n"
    assert gpu.parse_nvidia_smi(text) == [
        {"name": "NVIDIA GeForce RTX 4090", "driver": "560.94", "memory_mb": 24564},
        {"name": "NVIDIA RTX A6000", "driver": "550.54.15", "memory_mb": None},
        {"name": "Tesla, Inc. GPU", "driver": "1.0", "memory_mb": 8},
    ]
    assert gpu.parse_nvidia_smi("") == []
    assert gpu.parse_nvidia_smi("garbage line\n") == []


def test_nvidia_gpus_runs_nvidia_smi():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return NS(returncode=0, stdout="NVIDIA GeForce RTX 3060, 551.23, 12288\n")

    assert gpu.nvidia_gpus(run) == [{"name": "NVIDIA GeForce RTX 3060", "driver": "551.23", "memory_mb": 12288}]
    assert calls == [gpu.NVIDIA_SMI_QUERY]


def test_nvidia_gpus_without_driver():
    # nvidia-smi present but no driver loaded (exit 9), or not installed at all.
    assert gpu.nvidia_gpus(lambda cmd, **kw: NS(returncode=9, stdout="")) == []

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert gpu.nvidia_gpus(missing) == []

    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 15)

    assert gpu.nvidia_gpus(hangs) == []


def test_cuda_info_from_ctranslate2(monkeypatch):
    fake = types.SimpleNamespace(
        __version__="4.8.3",
        get_cuda_device_count=lambda: 2,
        get_supported_compute_types=lambda device: {"float16", "int8", "float32"},
    )
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    assert gpu.cuda_info() == {
        "ctranslate2": "4.8.3",
        "cuda_devices": 2,
        "cuda_compute_types": ["float16", "float32", "int8"],
    }

    def no_driver():
        raise RuntimeError("CUDA driver version is insufficient")

    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(__version__="4.8.3", get_cuda_device_count=no_driver))
    info = gpu.cuda_info()
    assert info["cuda_devices"] == 0 and "insufficient" in info["error"]

    monkeypatch.setitem(sys.modules, "ctranslate2", None)  # import fails
    info = gpu.cuda_info()
    assert info["ctranslate2"] is None and info["cuda_devices"] == 0 and "error" in info


# --------------------------------------------------------------------------- DLL / .so registration

WINDOWS_WHEEL_FILES = {
    "cublas/bin": ["cublas64_12.dll", "cublasLt64_12.dll", "nvblas64_12.dll", "__init__.py"],
    "cudnn/bin": ["cudnn64_9.dll", "cudnn_graph64_9.dll", "cudnn_ops64_9.dll"],
    "cuda_nvrtc/bin": ["nvrtc64_120_0.dll", "nvrtc64_120_0.alt.dll", "nvrtc-builtins64_129.dll"],
    "cufft/bin": ["cufft64_11.dll"],  # not ours
}
LINUX_WHEEL_FILES = {
    "cublas/lib": ["libcublas.so.12", "libcublasLt.so.12", "libnvblas.so.12"],
    "cudnn/lib": ["libcudnn.so.9", "libcudnn_graph.so.9"],
    "cuda_nvrtc/lib": ["libnvrtc.so.12", "libnvrtc.alt.so.12", "libnvrtc-builtins.so.12.9"],
}


def _fake_nvidia(root: Path, files: dict[str, list[str]]) -> Path:
    """A site-packages/nvidia tree laid out like the nvidia-*-cu12 wheels (see docs/GPU.md)."""
    nvidia = root / "nvidia"
    for sub, names in files.items():
        (nvidia / sub).mkdir(parents=True, exist_ok=True)
        for name in names:
            (nvidia / sub / name).write_text("")
    return nvidia


@pytest.fixture
def loads(monkeypatch):
    """Records ctypes loads instead of loading anything."""
    calls = []
    monkeypatch.setattr(gpu, "_load_library", lambda path, mode: calls.append((Path(path).name, mode)) or path)
    return calls


@pytest.fixture
def dll_dirs(monkeypatch):
    """Records os.add_dll_directory calls (the function only exists on Windows)."""
    calls = []
    monkeypatch.setattr(os, "add_dll_directory", lambda path: calls.append(path) or path, raising=False)
    return calls


def test_library_dirs_per_platform(tmp_path):
    nvidia = _fake_nvidia(tmp_path, {**WINDOWS_WHEEL_FILES, **LINUX_WHEEL_FILES})
    assert gpu.library_dirs([nvidia], "win32") == [nvidia / "cublas/bin", nvidia / "cuda_nvrtc/bin", nvidia / "cudnn/bin"]
    assert gpu.library_dirs([nvidia], "linux") == [nvidia / "cublas/lib", nvidia / "cuda_nvrtc/lib", nvidia / "cudnn/lib"]
    assert gpu.library_dirs([tmp_path / "missing"], "linux") == []


def test_runtime_libraries_skip_nvblas_alt_and_other_files(tmp_path):
    nvidia = _fake_nvidia(tmp_path, {**WINDOWS_WHEEL_FILES, **LINUX_WHEEL_FILES})
    win = [p.name for p in gpu.runtime_libraries(gpu.library_dirs([nvidia], "win32"), "win32")]
    assert win == [
        "cublas64_12.dll", "cublasLt64_12.dll",
        "nvrtc-builtins64_129.dll", "nvrtc64_120_0.dll",
        "cudnn64_9.dll", "cudnn_graph64_9.dll", "cudnn_ops64_9.dll",
    ]
    linux = [p.name for p in gpu.runtime_libraries(gpu.library_dirs([nvidia], "linux"), "linux")]
    assert linux == [
        "libcublas.so.12", "libcublasLt.so.12",
        "libnvrtc-builtins.so.12.9", "libnvrtc.so.12",
        "libcudnn.so.9", "libcudnn_graph.so.9",
    ]


def test_register_windows_adds_dll_directories_and_path(tmp_path, monkeypatch, loads, dll_dirs):
    nvidia = _fake_nvidia(tmp_path, WINDOWS_WHEEL_FILES)
    dirs = gpu.library_dirs([nvidia], "win32")
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    result = gpu.register(dirs, "win32")

    assert dll_dirs == [str(d) for d in dirs]
    assert os.environ["PATH"].split(os.pathsep) == [str(d) for d in dirs] + ["/usr/bin", "/bin"]
    assert result["dirs"] == [str(d) for d in dirs]
    assert "cublas64_12.dll" in result["loaded"] and "cudnn64_9.dll" in result["loaded"]
    assert {mode for _, mode in loads} == {0}
    # Registering again doesn't grow PATH.
    gpu.register(dirs, "win32")
    assert os.environ["PATH"].split(os.pathsep) == [str(d) for d in dirs] + ["/usr/bin", "/bin"]


def test_register_linux_preloads_globally(tmp_path, monkeypatch, loads, dll_dirs):
    nvidia = _fake_nvidia(tmp_path, LINUX_WHEEL_FILES)
    monkeypatch.setenv("PATH", "/usr/bin")

    result = gpu.register(gpu.library_dirs([nvidia], "linux"), "linux")

    assert dll_dirs == []  # LD_LIBRARY_PATH can't be changed in-process: preloading stands in for it
    assert os.environ["PATH"] == "/usr/bin"
    assert {mode for _, mode in loads} == {ctypes.RTLD_GLOBAL}
    assert result["loaded"] == [name for name, _ in loads]
    assert "libnvblas.so.12" not in result["loaded"]


def test_preload_retries_dependencies_and_skips_unloadable(tmp_path, monkeypatch):
    libs = [tmp_path / n for n in ("libcublas.so.12", "libcublasLt.so.12", "libcudnn_engines.so.9")]
    loaded = set()

    def load(path, mode):
        name = Path(path).name
        if name == "libcublas.so.12" and "libcublasLt.so.12" not in loaded:
            raise OSError("libcublasLt.so.12: cannot open shared object file")
        if name == "libcudnn_engines.so.9":
            raise OSError("libnvrtc.so.12: cannot open shared object file")
        loaded.add(name)

    monkeypatch.setattr(gpu, "_load_library", load)
    assert [p.name for p in gpu._preload(libs, "linux")] == ["libcublasLt.so.12", "libcublas.so.12"]


def test_setup_uses_site_packages_once_and_never_raises(tmp_path, monkeypatch, loads, dll_dirs):
    windows = sys.platform == "win32"
    nvidia = _fake_nvidia(tmp_path, WINDOWS_WHEEL_FILES if windows else LINUX_WHEEL_FILES)
    sub = "bin" if windows else "lib"
    monkeypatch.setattr(gpu, "_setup_result", None)
    monkeypatch.setattr(gpu, "nvidia_roots", lambda: [nvidia])
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # restored afterwards; setup() edits it on Windows

    first = gpu.setup()
    assert first["dirs"] == [str(nvidia / "cublas" / sub), str(nvidia / "cuda_nvrtc" / sub), str(nvidia / "cudnn" / sub)]
    assert len(first["loaded"]) == (7 if windows else 6)
    count = len(loads)
    assert gpu.setup() is first and len(loads) == count  # cached: nothing loaded twice

    def broken():
        raise PermissionError("site-packages unreadable")

    monkeypatch.setattr(gpu, "_setup_result", None)
    monkeypatch.setattr(gpu, "nvidia_roots", broken)
    assert gpu.setup() == {"dirs": [], "loaded": [], "error": "PermissionError: site-packages unreadable"}


def test_nvidia_roots_finds_namespace_package(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    _fake_nvidia(site, LINUX_WHEEL_FILES)
    monkeypatch.syspath_prepend(str(site))
    assert site / "nvidia" in gpu.nvidia_roots()


def test_nvidia_roots_without_the_extra(monkeypatch):
    monkeypatch.setattr(gpu.importlib.util, "find_spec", lambda name: None)
    assert gpu.nvidia_roots() == []


# --------------------------------------------------------------------------- transcribe hooks


class FakeModel:
    """faster_whisper.WhisperModel whose CUDA copy loads fine but fails on first use, like a missing cuBLAS."""

    instances = []
    cuda_error = "Library libcublas.so.12 is not found or cannot be loaded"

    def __init__(self, model, device="auto", device_index=0, compute_type="default"):
        self.device = device
        FakeModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        def segments():
            if self.device == "cuda":
                raise RuntimeError(FakeModel.cuda_error)
            yield NS(end=1.0, words=[NS(word=" hi", start=0.0, end=1.0, probability=0.9)])

        return segments(), NS(language="en", duration=1.0)


@pytest.fixture
def fake_model(monkeypatch):
    FakeModel.instances = []
    FakeModel.cuda_error = "Library libcublas.so.12 is not found or cannot be loaded"
    events = []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(gpu, "setup", lambda: events.append("setup") or {"dirs": [], "loaded": []})
    monkeypatch.setattr(tr, "_cuda_available", lambda: True)
    return events


@pytest.mark.parametrize("device,expected", [("auto", ["setup"]), ("cuda", ["setup"]), ("cuda:1", ["setup"]), ("cpu", [])])
def test_load_model_registers_cuda_runtime_first(fake_model, monkeypatch, device, expected):
    monkeypatch.setitem(
        sys.modules, "faster_whisper",
        types.SimpleNamespace(WhisperModel=lambda *a, **kw: fake_model.append("model")),
    )
    tr._load_model("tiny", device)
    assert fake_model == expected + ["model"]


def test_auto_falls_back_to_cpu_when_cuda_fails_on_first_use(fake_model):
    language, duration, words = tr.transcribe("in.wav", device="auto")
    assert [m.device for m in FakeModel.instances] == ["cuda", "cpu"]
    assert [w.text for w in words] == ["hi"] and language == "en"


def test_explicit_cuda_first_use_failure_is_raised(fake_model):
    with pytest.raises(RuntimeError, match="libcublas"):
        tr.transcribe("in.wav", device="cuda")
    assert [m.device for m in FakeModel.instances] == ["cuda"]


def test_unrelated_first_use_error_is_not_retried(fake_model):
    FakeModel.cuda_error = "Invalid data found when processing input"
    with pytest.raises(RuntimeError, match="Invalid data"):
        tr.transcribe("in.wav", device="auto")
    assert [m.device for m in FakeModel.instances] == ["cuda"]


# --------------------------------------------------------------------------- doctor


def test_doctor_prints_detection_as_json(monkeypatch, capsys):
    monkeypatch.setattr(gpu, "setup", lambda: {"dirs": ["/sp/nvidia/cublas/lib"], "loaded": ["libcublas.so.12"]})
    monkeypatch.setattr(gpu, "nvidia_gpus", lambda: [{"name": "NVIDIA GeForce RTX 4090", "driver": "560.94", "memory_mb": 24564}])
    monkeypatch.setattr(gpu, "cuda_info", lambda: {"ctranslate2": "4.8.3", "cuda_devices": 1})
    monkeypatch.setattr(tr, "_cuda_available", lambda: True)
    monkeypatch.setattr(tr, "_backend_available", lambda name: name == "whispercpp")

    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["nvidia_gpus"][0]["name"] == "NVIDIA GeForce RTX 4090"
    assert report["cuda_runtime"]["loaded"] == ["libcublas.so.12"]
    assert report["cuda"]["cuda_devices"] == 1
    assert report["backends"]["mlx"] is False and report["backends"]["whispercpp"] is True
    assert report["backend"] == "faster-whisper"
    assert report["device"] == {"device": "cuda", "index": 0, "compute_type": "float16"}
    assert report["platform"] == sys.platform


def test_doctor_without_gpu(monkeypatch, capsys):
    monkeypatch.setattr(gpu, "setup", lambda: {"dirs": [], "loaded": []})
    monkeypatch.setattr(gpu, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(gpu, "cuda_info", lambda: {"ctranslate2": "4.8.3", "cuda_devices": 0})
    monkeypatch.setattr(tr, "_cuda_available", lambda: False)
    monkeypatch.setattr(tr, "_backend_available", lambda name: False)

    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["nvidia_gpus"] == []
    assert report["backend"] == "faster-whisper"
    assert report["device"] == {"device": "cpu", "index": 0, "compute_type": "int8"}


# --------------------------------------------------------------------------- installers' GPU decision

GPU_LISTING = "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-12345678-1234-1234-1234-123456789abc)"
NO_DRIVER = "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."


def _fake_bin(tmp_path: Path, smi: str | None) -> Path:
    """A PATH dir holding only a fake nvidia-smi: "gpu" lists a GPU, "nodriver" fails, None: no nvidia-smi."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if smi is not None:
        body = f"echo '{GPU_LISTING}'" if smi == "gpu" else f"echo \"{NO_DRIVER}\"; exit 9"
        (bin_dir / "nvidia-smi").write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / "nvidia-smi").chmod(0o755)
    return bin_dir


def _gpu_choice_sh(tmp_path: Path, os_name: str, smi: str | None, gpu_env: str | None = None):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not installed")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BETTER_CAPTIONS_")}
    env.update(BETTER_CAPTIONS_LIB="1", PATH=str(_fake_bin(tmp_path, smi)))  # nothing else: no real nvidia-smi
    if gpu_env is not None:
        env["BETTER_CAPTIONS_GPU"] = gpu_env
    return subprocess.run(
        [bash, "-c", 'source "$1"; gpu_choice "$2"', "_", str(INSTALL / "install.sh"), os_name],
        env=env, capture_output=True, text=True, timeout=60,
    )


sh_only = pytest.mark.skipif(sys.platform == "win32", reason="bash installer")


@sh_only
@pytest.mark.parametrize(
    "os_name,smi,gpu_env,expected",
    [
        ("linux", "gpu", None, "cuda"),
        ("linux", "gpu", "auto", "cuda"),
        ("linux", "nodriver", None, "none"),
        ("linux", None, None, "none"),
        ("linux", "gpu", "none", "none"),
        ("linux", None, "cuda", "cuda"),
        ("macos", "gpu", None, "none"),
    ],
)
def test_install_sh_gpu_choice(tmp_path, os_name, smi, gpu_env, expected):
    result = _gpu_choice_sh(tmp_path, os_name, smi, gpu_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@sh_only
def test_install_sh_cuda_override_on_macos_warns(tmp_path):
    result = _gpu_choice_sh(tmp_path, "macos", None, "cuda")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "none"
    assert "CUDA isn't available on macOS" in result.stderr


@sh_only
def test_install_sh_rejects_bad_gpu_value(tmp_path):
    result = _gpu_choice_sh(tmp_path, "linux", "gpu", "rocm")
    assert result.returncode != 0
    assert "BETTER_CAPTIONS_GPU must be auto, cuda or none" in result.stderr


def _gpu_choice_ps1(tmp_path: Path, smi: str | None, gpu_env: str | None = None):
    pwsh = os.environ.get("PWSH") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BETTER_CAPTIONS_")}
    env.update(BETTER_CAPTIONS_LIB="1", PATH=str(_fake_bin(tmp_path, smi)))
    if gpu_env is not None:
        env["BETTER_CAPTIONS_GPU"] = gpu_env
    script = f"& '{INSTALL / 'install.ps1'}'; try {{ Get-GpuChoice }} catch {{ Write-Output \"error: $_\"; exit 3 }}"
    return subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                          env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(sys.platform == "win32", reason="fakes nvidia-smi with a shell script; Windows has real WMI adapters")
@pytest.mark.parametrize(
    "smi,gpu_env,expected",
    [("gpu", None, "cuda"), ("nodriver", None, "none"), (None, None, "none"), ("gpu", "none", "none"), (None, "cuda", "cuda")],
)
def test_install_ps1_gpu_choice(tmp_path, smi, gpu_env, expected):
    result = _gpu_choice_ps1(tmp_path, smi, gpu_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="see test_install_ps1_gpu_choice")
def test_install_ps1_rejects_bad_gpu_value(tmp_path):
    result = _gpu_choice_ps1(tmp_path, "gpu", "rocm")
    assert result.returncode == 3
    assert "BETTER_CAPTIONS_GPU must be auto, cuda or none" in result.stdout
