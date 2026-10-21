"""whispercpp backend with whisper-cli, downloads and the Vulkan loader replaced by fakes."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
import soundfile as sf

from better_captions.engine.backends import whispercpp as wc

# --- helpers -----------------------------------------------------------------------


def tok(text, start_ms, end_ms, p=0.9, dtw=-1):
    return {"text": text, "offsets": {"from": start_ms, "to": end_ms}, "id": 1, "p": p, "t_dtw": dtw}


def full_json(segments, language="en"):
    return {
        "systeminfo": "x",
        "model": {"type": "tiny"},
        "params": {"model": "m", "language": "auto", "translate": False},
        "result": {"language": language},
        "transcription": [
            {"timestamps": {"from": "0", "to": "0"}, "offsets": {"from": s, "to": e}, "text": "", "tokens": toks}
            for s, e, toks in segments
        ],
    }


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BETTER_CAPTIONS_VULKAN", raising=False)
    wc._help_cache.clear()
    return tmp_path / "home"


@pytest.fixture
def fake_downloads(monkeypatch):
    fetched = []
    payloads = {}

    def urlopen(url, timeout=None):
        fetched.append(url)
        if url not in payloads:
            raise OSError(f"unexpected url {url}")
        return io.BytesIO(payloads[url])

    monkeypatch.setattr(wc.urllib.request, "urlopen", urlopen)
    return NS(fetched=fetched, payloads=payloads)


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def make_tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def pin(monkeypatch, name, payload, platform="linux"):
    """Pretend the pinned asset for this platform is `payload` (named `name`)."""
    asset = wc.Asset(name, hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(wc.sys, "platform", platform)
    monkeypatch.setattr(wc.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(wc, "ASSETS", {(platform, "x86_64"): asset})
    monkeypatch.setattr(wc, "BINARY_NAME", "whisper-cli.exe" if platform == "win32" else "whisper-cli")
    return asset


# --- JSON -> words ---------------------------------------------------------------------


def test_parse_result_words_from_token_offsets():
    data = full_json(
        [
            (0, 7940, [tok("[_BEG_]", 0, 0, p=0.8), tok(" And", 320, 400, p=0.6), tok(" so", 400, 520, p=1.0), tok("[_TT_397]", 7940, 7940, p=0.01)]),
            (7940, 10760, [tok(" country", 10000, 10740, p=0.9), tok(".", 10740, 10760, p=0.5), tok("[_TT_538]", 10760, 10760)]),
        ]
    )
    language, words, last_end = wc.parse_result(data)
    assert language == "en" and last_end == 10.76
    assert [(w.text, w.start, w.end, w.speaker) for w in words] == [
        ("And", 0.32, 0.4, None),
        ("so", 0.4, 0.52, None),
        ("country.", 10.0, 10.76, None),
    ]
    assert [round(w.probability, 2) for w in words] == [0.6, 1.0, 0.7]  # mean over the merged tokens


def test_merge_sub_word_tokens_and_punctuation():
    words = wc.merge_tokens([tok(" Hel", 0, 100, p=0.8), tok("lo", 100, 200, p=0.6), tok(",", 200, 210), tok(" wor", 300, 400), tok("ld", 400, 500), tok("!", 500, 510)])
    assert [(w.text, w.start, w.end) for w in words] == [("Hello,", 0.0, 0.21), ("world!", 0.3, 0.51)]
    assert words[0].probability == pytest.approx((0.8 + 0.6 + 0.9) / 3)


def test_merge_skips_specials_and_blank_tokens_and_handles_missing_offsets():
    words = wc.merge_tokens([tok("[_SOT_]", 0, 0), tok(" ", 0, 10), {"text": " a", "p": 0.5}, tok("b", 100, 200)])
    assert [(w.text, w.start, w.end) for w in words] == [("ab", 0.1, 0.2)]


def test_no_space_languages_split_every_token():
    data = full_json([(0, 1000, [tok("你", 0, 300), tok("好", 300, 600)])], language="zh")
    _, words, _ = wc.parse_result(data)
    assert [w.text for w in words] == ["你", "好"]
    assert [w.text for w in wc.merge_tokens([tok("你", 0, 300), tok("好", 300, 600)])] == ["你好"]


def test_dtw_times_are_used_when_present():
    # t_dtw is in centiseconds and marks roughly where the token ends.
    words = wc.merge_tokens(
        [tok(" And", 320, 400, dtw=76), tok(" so", 400, 520, dtw=106), tok(" ask", 3290, 3650, dtw=388), tok(" not", 4000, 4300, dtw=458), tok(".", 4300, 4310, dtw=458)]
    )
    assert [(w.text, w.start, w.end) for w in words] == [
        ("And", 0.32, 0.76),  # first word: whisper's own start estimate
        ("so", 0.76, 1.06),  # starts where the previous word ended
        ("ask", 3.29, 3.88),  # the pause after "so" is not glued on: start estimate wins
        ("not.", 4.0, 4.58),
    ]


def test_dtw_falls_back_to_offsets_when_missing_on_a_token():
    words = wc.merge_tokens([tok(" a", 0, 100, dtw=20), tok(" b", 100, 200, dtw=-1), tok(" c", 200, 300, dtw=40)])
    assert [(w.start, w.end) for w in words] == [(0.0, 0.2), (0.1, 0.2), (0.2, 0.4)]


def test_parse_result_language_fallbacks():
    assert wc.parse_result({"params": {"language": "de"}, "transcription": []}) == ("de", [], 0.0)
    assert wc.parse_result({}) == ("en", [], 0.0)


# --- progress / command line --------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("whisper_print_progress_callback: progress =   5%\n", 0.05),
        ("whisper_print_progress_callback: progress = 100%\n", 1.0),
        ("ggml_vulkan: Found 1 Vulkan devices:\n", None),
        ("", None),
    ],
)
def test_parse_progress_line(line, expected):
    assert wc.parse_progress_line(line) == expected


def test_build_command(monkeypatch):
    monkeypatch.setattr(wc.os, "cpu_count", lambda: 32)
    cmd = wc.build_command(Path("/b/whisper-cli"), Path("/m/ggml-small.bin"), "in.wav", None, "auto", Path("/t/out"), "small")
    assert cmd == ["/b/whisper-cli", "-m", "/m/ggml-small.bin", "-f", "in.wav", "-l", "auto", "-ojf", "-of", "/t/out", "-pp", "-np", "-t", "8", "-dtw", "small", "-nfa"]
    cmd = wc.build_command(Path("w"), Path("m"), "in.wav", "en", "vulkan:1", Path("o"), None)
    assert "-dtw" not in cmd and "-nfa" not in cmd
    assert cmd[cmd.index("-l") + 1] == "en" and cmd[-2:] == ["-dev", "1"]


@pytest.mark.parametrize("device,expected", [("cpu", ["-ng"]), ("auto", []), ("vulkan", []), ("gpu:2", ["-dev", "2"]), ("cuda:1", ["-dev", "1"])])
def test_device_args(device, expected):
    assert wc.device_args(device) == expected


def test_unknown_device_rejected():
    with pytest.raises(ValueError):
        wc.device_args("tpu")


# --- models ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [("large-v3-turbo", "ggml-large-v3-turbo.bin"), ("small.en", "ggml-small.en.bin"), ("large", "ggml-large-v3.bin"), ("turbo", "ggml-large-v3-turbo.bin"), ("medium-q5_0", "ggml-medium-q5_0.bin")],
)
def test_ggml_model_name(model, expected):
    assert wc.ggml_model_name(model) == expected


def test_unknown_model_rejected():
    with pytest.raises(ValueError, match="unknown whisper.cpp model"):
        wc.ggml_model_name("distil-large-v3")


@pytest.mark.parametrize(
    "model,preset",
    [("large-v3-turbo", "large.v3.turbo"), ("large-v3-turbo-q8_0", "large.v3.turbo"), ("tiny.en", "tiny.en"), ("large", "large.v3"), ("/models/custom.bin", None)],
)
def test_dtw_preset(model, preset):
    assert wc.dtw_preset(model) == preset


def test_ensure_model_downloads_into_cache_once(home, fake_downloads):
    url = f"{wc.MODEL_URL}/ggml-large-v3-turbo.bin"
    fake_downloads.payloads[url] = b"GGML"
    path = wc.ensure_model("large-v3-turbo")
    assert path == home / "models" / "whispercpp" / "ggml-large-v3-turbo.bin"
    assert path.read_bytes() == b"GGML"
    assert wc.ensure_model("large-v3-turbo") == path
    assert fake_downloads.fetched == [url]


def test_ensure_model_accepts_existing_bin_path(home, fake_downloads, tmp_path):
    mine = tmp_path / "ggml-mine.bin"
    mine.write_bytes(b"x")
    assert wc.ensure_model(str(mine)) == mine
    assert fake_downloads.fetched == []


# --- binary download + checksum -----------------------------------------------------------------


def test_ensure_binary_verifies_checksum_and_extracts_zip(home, fake_downloads, monkeypatch):
    payload = make_zip({"whisper-cli.exe": b"EXE", "ggml-vulkan.dll": b"DLL"})
    asset = pin(monkeypatch, "whisper-win.zip", payload, platform="win32")
    fake_downloads.payloads[asset.url] = payload
    binary = wc.ensure_binary()
    assert binary == home / "bin" / f"whispercpp-{wc.RELEASE}" / "whisper-cli.exe"
    assert binary.read_bytes() == b"EXE" and (binary.parent / "ggml-vulkan.dll").exists()
    assert not (binary.parent / "whisper-win.zip").exists()  # archive removed after extraction
    assert wc.ensure_binary() == binary
    assert fake_downloads.fetched == [asset.url]  # cached: not fetched again


def test_ensure_binary_finds_cli_inside_tarball_folder(home, fake_downloads, monkeypatch):
    payload = make_tar({"whisper-v1/whisper-cli": b"ELF", "whisper-v1/libwhisper.so.1": b"SO", "../escape": b"no"})
    asset = pin(monkeypatch, "whisper-linux.tar.gz", payload)
    fake_downloads.payloads[asset.url] = payload
    binary = wc.ensure_binary()
    assert binary == home / "bin" / f"whispercpp-{wc.RELEASE}" / "whisper-v1" / "whisper-cli"
    assert binary.stat().st_mode & 0o111
    assert not (home / "bin" / "escape").exists() and not (home / "escape").exists()
    env = wc._environment(binary)
    assert env["LD_LIBRARY_PATH"].split(wc.os.pathsep)[0] == str(binary.parent)


def test_checksum_mismatch_removes_download_and_raises(home, fake_downloads, monkeypatch):
    asset = pin(monkeypatch, "whisper-linux.tar.gz", make_tar({"whisper-cli": b"good"}))
    fake_downloads.payloads[asset.url] = make_tar({"whisper-cli": b"tampered"})
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        wc.ensure_binary()
    assert not [p for p in (home / "bin").rglob("*") if p.is_file()]  # neither the archive nor a .part file is kept
    assert wc.binary_path() is None


def test_download_failure_leaves_no_partial_file(home, fake_downloads, monkeypatch):
    asset = pin(monkeypatch, "whisper-linux.tar.gz", b"x")
    with pytest.raises(RuntimeError, match="could not download"):
        wc.ensure_binary()
    assert not [p for p in (home / "bin").rglob("*") if p.is_file()]


def test_ensure_binary_without_asset_for_platform(home, monkeypatch):
    monkeypatch.setattr(wc.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="no prebuilt Vulkan binary"):
        wc.ensure_binary()


# --- available() -------------------------------------------------------------------------


@pytest.fixture
def no_downloads(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("available() must never download")

    monkeypatch.setattr(wc.urllib.request, "urlopen", boom)


def gpu_setup(monkeypatch, devices, platform="linux", machine="x86_64"):
    monkeypatch.setattr(wc.sys, "platform", platform)
    monkeypatch.setattr(wc.platform, "machine", lambda: machine)
    if isinstance(devices, Exception):

        def probe():
            raise devices

    else:

        def probe():
            return devices

    monkeypatch.setattr(wc, "_vulkan_devices", probe)


def test_available_with_gpu(home, no_downloads, monkeypatch):
    gpu_setup(monkeypatch, [("AMD Radeon RX 7800 XT (RADV NAVI32)", 2)])
    assert wc.available() is True
    gpu_setup(monkeypatch, [("Intel(R) Arc(TM) A770", 2)], platform="win32", machine="AMD64")
    assert wc.available() is True


def test_available_false_with_only_software_device(home, no_downloads, monkeypatch):
    gpu_setup(monkeypatch, [("llvmpipe (LLVM 17.0.6, 256 bits)", wc.VK_PHYSICAL_DEVICE_TYPE_CPU)])
    assert wc.available() is False
    gpu_setup(monkeypatch, [])
    assert wc.available() is False


def test_available_false_without_loader_or_binary(home, no_downloads, monkeypatch):
    gpu_setup(monkeypatch, OSError("no Vulkan loader"))
    assert wc.available() is False
    gpu_setup(monkeypatch, [("GPU", 2)], platform="darwin", machine="arm64")
    assert wc.available() is False
    gpu_setup(monkeypatch, [("GPU", 2)], platform="linux", machine="aarch64")
    assert wc.available() is False


def test_available_env_override(home, no_downloads, monkeypatch):
    gpu_setup(monkeypatch, [])
    monkeypatch.setenv("BETTER_CAPTIONS_VULKAN", "1")
    assert wc.available() is True
    gpu_setup(monkeypatch, [("GPU", 2)])
    monkeypatch.setenv("BETTER_CAPTIONS_VULKAN", "0")
    assert wc.available() is False


def test_supports_dtw_reads_help_once(monkeypatch, home):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return NS(stdout="  -dtw MODEL --dtw MODEL  [ ] compute token-level timestamps\n", stderr="", returncode=0)

    monkeypatch.setattr(wc.subprocess, "run", run)
    assert wc.supports_dtw(Path("/x/whisper-cli")) is True
    assert wc.supports_dtw(Path("/x/whisper-cli")) is True
    assert calls == [["/x/whisper-cli", "--help"]]
    monkeypatch.setattr(wc.subprocess, "run", lambda cmd, **k: NS(stdout="usage: old", stderr="", returncode=0))
    assert wc.supports_dtw(Path("/y/whisper-cli")) is False


# --- transcribe() with a fake whisper-cli ---------------------------------------------------


PROGRESS_LINES = [
    "ggml_vulkan: Found 1 Vulkan devices:\n",
    "whisper_print_progress_callback: progress =   5%\n",
    "whisper_print_progress_callback: progress =  50%\n",
    "whisper_print_progress_callback: progress =  50%\n",
    "whisper_print_progress_callback: progress = 100%\n",
]


class FakePopen:
    """Stands in for whisper-cli: writes the JSON next to -of, streams progress on stderr."""

    calls = []
    result = None
    returncode = 0
    stderr_lines = PROGRESS_LINES

    def __init__(self, cmd, **kwargs):
        FakePopen.calls.append(NS(cmd=cmd, kwargs=kwargs))
        out = Path(cmd[cmd.index("-of") + 1])
        out.with_name(out.name + ".json").write_text(json.dumps(self.result), encoding="utf-8")
        self.stderr = iter(self.stderr_lines)

    def wait(self):
        return self.returncode


@pytest.fixture
def fake_cli(home, monkeypatch, tmp_path):
    FakePopen.calls = []
    FakePopen.returncode = 0
    FakePopen.stderr_lines = PROGRESS_LINES
    FakePopen.result = full_json([(0, 2000, [tok(" Hello", 0, 500, p=0.9), tok(" world", 600, 2000, p=0.8), tok(".", 2000, 2000, p=0.4)])], language="en")
    monkeypatch.setattr(wc.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(wc.subprocess, "run", lambda cmd, **k: NS(stdout="--dtw", stderr="", returncode=0))
    binary = home / "bin" / f"whispercpp-{wc.RELEASE}" / wc.BINARY_NAME
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"bin")
    model = home / "models" / "whispercpp" / "ggml-small.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"ggml")
    audio = tmp_path / "in.wav"
    sf.write(audio, np.zeros(16000 * 3, dtype=np.float32), 16000)
    return NS(binary=binary, model=model, audio=str(audio))


def test_transcribe_runs_cli_and_parses_output(fake_cli):
    seen = []
    language, duration, words = wc.transcribe(fake_cli.audio, model="small", language=None, device="auto", progress=seen.append)
    assert language == "en" and duration == 3.0  # duration from the audio file, not the last segment
    assert [(w.text, w.start, w.end, round(w.probability, 2), w.speaker) for w in words] == [("Hello", 0.0, 0.5, 0.9, None), ("world.", 0.6, 2.0, 0.6, None)]
    assert seen == [0.05, 0.5, 1.0]  # repeated 50% not re-reported
    (call,) = FakePopen.calls
    assert call.cmd[0] == str(fake_cli.binary)
    assert call.cmd[call.cmd.index("-m") + 1] == str(fake_cli.model)
    assert call.cmd[call.cmd.index("-f") + 1] == fake_cli.audio
    assert call.cmd[call.cmd.index("-l") + 1] == "auto"
    assert "-ojf" in call.cmd and "-pp" in call.cmd
    assert call.cmd[call.cmd.index("-dtw") + 1] == "small" and "-nfa" in call.cmd
    assert "-ng" not in call.cmd
    assert call.kwargs["stderr"] is subprocess.PIPE


def test_transcribe_cpu_language_and_duration_fallback(fake_cli, monkeypatch):
    monkeypatch.setattr(wc, "audio_duration", lambda path: None)
    FakePopen.stderr_lines = ["whisper_print_progress_callback: progress =  40%\n"]
    seen = []
    language, duration, words = wc.transcribe(fake_cli.audio, model="small", language="de", device="cpu", progress=seen.append)
    assert language == "en" and duration == 2.0  # language from the JSON, duration = end of the last segment
    assert seen == [0.4, 1.0]  # completion reported even when whisper-cli stopped at 40%
    (call,) = FakePopen.calls
    assert "-ng" in call.cmd and call.cmd[call.cmd.index("-l") + 1] == "de"


def test_transcribe_without_dtw_support(fake_cli, monkeypatch):
    wc._help_cache.clear()
    monkeypatch.setattr(wc.subprocess, "run", lambda cmd, **k: NS(stdout="no such flag", stderr="", returncode=0))
    wc.transcribe(fake_cli.audio, model="small")
    (call,) = FakePopen.calls
    assert "-dtw" not in call.cmd and "-nfa" not in call.cmd


def test_transcribe_failure_raises_with_stderr(fake_cli):
    FakePopen.returncode = 3
    FakePopen.stderr_lines = ["error: failed to initialize whisper context\n"]
    with pytest.raises(RuntimeError, match="exit 3.*failed to initialize whisper context"):
        wc.transcribe(fake_cli.audio, model="small")


def test_transcribe_downloads_missing_model(fake_cli, fake_downloads):
    fake_downloads.payloads[f"{wc.MODEL_URL}/ggml-tiny.bin"] = b"TINY"
    wc.transcribe(fake_cli.audio, model="tiny")
    (call,) = FakePopen.calls
    assert call.cmd[call.cmd.index("-m") + 1] == str(fake_cli.model.with_name("ggml-tiny.bin"))
    assert fake_downloads.fetched == [f"{wc.MODEL_URL}/ggml-tiny.bin"]


def test_backend_module_imports_no_model_libraries():
    import sys

    code = (
        "import sys, better_captions.engine.backends.whispercpp; "
        "heavy = {'faster_whisper', 'ctranslate2', 'soundfile', 'numpy', 'torch'} & set(sys.modules); "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
