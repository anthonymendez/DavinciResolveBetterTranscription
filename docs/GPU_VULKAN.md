# GPU transcription on AMD, Intel (and NVIDIA) with whisper.cpp + Vulkan

The default transcription backend, faster-whisper, only uses NVIDIA GPUs (CUDA). The
`whispercpp` backend (`src/better_captions/engine/backends/whispercpp.py`) runs
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) built with its Vulkan GPU backend,
which works on any GPU whose driver supports Vulkan: AMD, Intel and NVIDIA, discrete or
integrated, on Windows and Linux.

`better-captions run ... --backend auto` picks it automatically when there is no CUDA GPU
and a Vulkan GPU is present (see `choose_backend` in `engine/transcribe.py`); force it with
`--backend whispercpp`. Nothing is downloaded until the first transcription.

## Where the binary comes from

Upstream ggml-org publishes no Vulkan builds (their release assets are CPU, BLAS, CUDA and
OpenCL-Adreno only, e.g. [release b5454](https://github.com/ggml-org/whisper.cpp/releases/tag/b5454)),
and no Python binding ships Vulkan wheels for both Windows and Linux. The AMD Lemonade SDK
team maintains [lemonade-sdk/whisper.cpp-rocm](https://github.com/lemonade-sdk/whisper.cpp-rocm),
a fork that tracks upstream releases and publishes prebuilt ROCm, Vulkan, NPU, CPU and Metal
archives. We pin its Vulkan builds:

| Platform | Archive (release [v1.8.4](https://github.com/lemonade-sdk/whisper.cpp-rocm/releases/tag/v1.8.4), upstream whisper.cpp v1.8.4) | SHA256 |
|---|---|---|
| Windows x64 | `whisper-v1.8.4-windows-vulkan-x64.zip` (21,203,995 bytes) | `e0d20a0f92e31b98adc0faf71172efc810b701e6391a9d858ca045bff26f77cd` |
| Linux x86_64 | `whisper-v1.8.4-linux-vulkan-x86_64.tar.gz` (43,770,408 bytes) | `55d0bdf9be7092ed8148b27d26353f5755446381eb9e1e5b6b00d5c761d813b1` |

The checksums were taken from the GitHub release API's asset digests
(`https://api.github.com/repos/lemonade-sdk/whisper.cpp-rocm/releases/tags/v1.8.4`) and
confirmed by hashing the downloaded archives (October 2026). The engine downloads the archive
for the current platform into `<data dir>/bin/whispercpp-v1.8.4/` (data dir: see
`docs/ARCHITECTURE.md`), verifies the SHA256 before extracting, and refuses (deleting the
file) on a mismatch. The Windows zip is flat (`whisper-cli.exe` + `ggml-vulkan.dll`,
`whisper.dll`, ...); the Linux tarball nests everything in a
`whisper-v1.8.4-linux-vulkan-x86_64/` folder. The Linux binaries' RUNPATH points at the CI
build tree, so the engine sets `LD_LIBRARY_PATH` to the binary's folder when launching it.

macOS and ARM are not covered (Apple Silicon uses the `mlx` backend). To update the pin,
change `RELEASE` and `ASSETS` in the backend module and re-verify the hashes.

### Licence

- whisper.cpp and the whisper.cpp-rocm fork are MIT licensed
  ([whisper.cpp LICENSE](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE),
  copyright Georgi Gerganov and contributors; the fork's README restates MIT).
- The Vulkan runtime is not bundled: the archives link against the system Vulkan loader
  (`vulkan-1.dll` / `libvulkan.so.1`), which GPU drivers install.
- Models: the ggml conversions in
  [ggerganov/whisper.cpp on Hugging Face](https://huggingface.co/ggerganov/whisper.cpp) are
  MIT licensed, like OpenAI's original [Whisper](https://github.com/openai/whisper) weights.

## GPU support

Vulkan is cross-vendor. whisper.cpp's README calls it a "cross-vendor solution" whose only
requirement is a driver that supports the Vulkan API
([whisper.cpp README, Vulkan GPU support](https://github.com/ggml-org/whisper.cpp#vulkan-gpu-support));
the whisper.cpp-rocm README lists "any GPU with a Vulkan 1.3-capable driver: AMD, NVIDIA,
Intel". In practice:

| GPU | Driver with Vulkan |
|---|---|
| AMD Radeon (RDNA/GCN, incl. Ryzen iGPUs) | Adrenalin (Windows), Mesa RADV or AMDGPU-PRO (Linux) |
| Intel Arc / Iris Xe / UHD | Intel graphics driver (Windows), Mesa ANV (Linux) |
| NVIDIA GeForce / RTX | GeForce driver (Windows and Linux); prefer the CUDA faster-whisper backend when it works |

`available()` is cheap and never downloads: it loads the system Vulkan loader with
`ctypes`, creates an instance and enumerates physical devices, and reports True only when at
least one device is not of type `VK_PHYSICAL_DEVICE_TYPE_CPU` (so Mesa's llvmpipe/lavapipe
software renderer does not count) and the platform has a pinned archive. Set
`BETTER_CAPTIONS_VULKAN=1` or `0` to override the probe (e.g. inside a container).
`--device cpu` runs whisper.cpp without the GPU (`-ng`); `--device vulkan:1` picks GPU 1
(`-dev 1`). ggml prints the devices it sees on stderr at start-up (`ggml_vulkan: Found N
Vulkan devices:` or `ggml_vulkan: No devices found.`).

## Models

Model names are the same as for faster-whisper (`large-v3-turbo` default, `large-v3`,
`medium`, `small`, `base`, `tiny`, `.en` variants, quantised `-q5_0`/`-q5_1`/`-q8_0` variants;
`large` means `large-v3`, `turbo` means `large-v3-turbo`). They map to
`https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<name>.bin`, the same files
whisper.cpp's `models/download-ggml-model.sh` fetches, cached in `<data dir>/models/whispercpp/`.
A path to a `ggml-*.bin` file is used directly. Model files are not checksummed (like the
diarization models). `large-v3-turbo` is 1.62 GB.

## Word timestamps, progress and language

- whisper-cli is run with `-ojf` (full JSON with one entry per token, with `offsets` in ms
  and probability `p`). Tokens are merged into words: a token that starts with whitespace
  begins a new word; word pieces and punctuation join the previous one; `[_BEG_]`/`[_TT_n]`
  specials are dropped, and so are non-speech annotations such as `[BLANK_AUDIO]`, `[MUSIC]`,
  `(music)`, `(applause)` and `*laughs*`. For languages written without spaces (zh, ja, th, lo, my) every token
  is a word. Word probability is the mean of its tokens'.
- When the binary's `--help` lists `-dtw` and the model has an alignment-head preset
  (`tiny`, `base`, `small`, `medium`, their `.en` variants, `large.v1`/`v2`/`v3`,
  `large.v3.turbo`; quantised models use their base model's preset), the engine passes
  `-dtw <preset> -nfa`: DTW over the cross-attention alignment heads (the method OpenAI's
  Whisper uses) gives each token a `t_dtw` time in centiseconds marking roughly where it ends.
  Flash attention has to be off for whisper.cpp to compute it (with it on, `t_dtw` is -1),
  which costs some speed. A word then ends at its last token's DTW time and starts where the
  previous word ended, unless whisper's own start estimate is later (so pauses are not glued
  onto the following word). Without DTW the plain token offsets are used.
  Set `BETTER_CAPTIONS_WHISPERCPP_DTW=0` to skip DTW and `-nfa` (and the `--help` probe) for
  speed, at the cost of word timing precision.
- `-pp` prints `whisper_print_progress_callback: progress =  N%` lines on stderr in 5 %
  steps; they are forwarded as progress fractions.
- `-l auto` (when no `--language` is given) makes whisper.cpp detect the language; the result
  is read from `result.language` in the JSON. English-only models ignore it.
- Duration comes from the audio file header (soundfile), falling back to the end of the last
  segment. whisper-cli reads wav, flac, mp3 and ogg and resamples to 16 kHz itself.

## Offline / manual install

Put the archive's contents under `<data dir>/bin/whispercpp-v1.8.4/` (the engine looks for
`whisper-cli[.exe]` at any depth there) and the `ggml-*.bin` model under
`<data dir>/models/whispercpp/`; nothing is downloaded when both exist.

## Sources

- ggml-org/whisper.cpp releases (no Vulkan assets): https://github.com/ggml-org/whisper.cpp/releases
- whisper.cpp README, Vulkan section and licence: https://github.com/ggml-org/whisper.cpp
- lemonade-sdk/whisper.cpp-rocm (prebuilt Vulkan/ROCm/NPU builds, MIT): https://github.com/lemonade-sdk/whisper.cpp-rocm
- Release v1.8.4 with asset digests: https://github.com/lemonade-sdk/whisper.cpp-rocm/releases/tag/v1.8.4
- whisper-cli flags (`-ojf`, `-pp`, `-dtw`, `-ng`, `-dev`): https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/cli.cpp
- `t_dtw` semantics ("roughly corresponds to the moment in audio in which the token was output"): https://github.com/ggml-org/whisper.cpp/blob/master/include/whisper.h
- ggml models and `download-ggml-model.sh`: https://huggingface.co/ggerganov/whisper.cpp, https://github.com/ggml-org/whisper.cpp/blob/master/models/download-ggml-model.sh
