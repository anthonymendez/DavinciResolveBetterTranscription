# Better Captions installer for Windows.
#
#   irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.ps1 | iex
#
# Re-running it upgrades an existing install. Needs admin rights (one UAC
# prompt) only to install python.org's Python for all users when Resolve
# has none.
# Install layout: see docs/ARCHITECTURE.md ("Install layout").
#
# Environment:
#   BETTER_CAPTIONS_HOME       data dir override (default: %LOCALAPPDATA%\BetterCaptions)
#   BETTER_CAPTIONS_REF        git ref (branch, tag or commit SHA) to install from GitHub (default: the
#                              latest release, or main if there is none or GitHub's API can't be reached)
#   BETTER_CAPTIONS_RELEASE_API  URL of GitHub's releases/latest API, or a local file with its JSON response (tests)
#   BETTER_CAPTIONS_SOURCE     install from this local source dir instead of GitHub (dev/tests)
#   BETTER_CAPTIONS_SKIP_DEPS  =1: install the package with --no-deps (tests only)
#   BETTER_CAPTIONS_GPU        auto (default): install the NVIDIA CUDA runtime when an NVIDIA GPU is
#                              found; cuda: always install it; none: never (see docs/GPU.md)
#   BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON  =1: don't check/install the Python Resolve runs the plugin with
#   BETTER_CAPTIONS_RESOLVE_PYTHONS     semicolon-separated Pythons to check instead of Resolve's locations (tests)
#   BETTER_CAPTIONS_LIB        =1: only define the functions (in the caller's session), don't install (tests)
#
# Runs via `iex`, so it never calls `exit` (that would close the user's
# shell): errors are thrown and reported by the try/catch at the bottom.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    $Repo = 'anthonymendez/DavinciResolveBetterTranscription'
    $PythonVersion = '3.12'
    # Last 3.12 release with python.org installers.
    $ResolvePythonRelease = '3.12.10'

    function Say([string]$Message) { Write-Host "==> $Message" }
    function Warn([string]$Message) { Write-Warning $Message }

    function Get-DataDir {
        if ($env:BETTER_CAPTIONS_HOME) { return $env:BETTER_CAPTIONS_HOME }
        return Join-Path $env:LOCALAPPDATA 'BetterCaptions'
    }

    function Get-ResolveUtilityDir {
        return Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
    }

    function Test-ResolveInstalled {
        $candidates = @(
            (Join-Path $env:ProgramFiles 'Blackmagic Design\DaVinci Resolve'),
            (Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve')
        )
        foreach ($c in $candidates) { if (Test-Path $c) { return $true } }
        return $false
    }

    function Invoke-Checked([string]$What, [scriptblock]$Command) {
        & $Command
        if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
    }

    function Ensure-Uv {
        if (Get-Command uv -ErrorAction SilentlyContinue) { return }
        $userBin = Join-Path $env:USERPROFILE '.local\bin'
        $env:Path = "$userBin;$env:Path"
        if (Get-Command uv -ErrorAction SilentlyContinue) { return }
        # The official installer puts uv in %USERPROFILE%\.local\bin (no admin needed).
        Say 'Installing uv (Python package manager) into %USERPROFILE%\.local\bin'
        Invoke-Checked 'uv install' {
            powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv was installed but is not on PATH; open a new terminal and re-run'
        }
    }

    # The latest release's tag from GitHub's releases/latest API, or $null if
    # there is no release or the API can't be reached (offline, rate-limited).
    # BETTER_CAPTIONS_RELEASE_API may name a local file holding the response (tests).
    function Get-LatestReleaseTag {
        $api = if ($env:BETTER_CAPTIONS_RELEASE_API) { $env:BETTER_CAPTIONS_RELEASE_API } else { "https://api.github.com/repos/$Repo/releases/latest" }
        try {
            if ($api -match '^https?://') {
                $release = Invoke-RestMethod -Uri $api -UseBasicParsing -TimeoutSec 20 -Headers @{ Accept = 'application/vnd.github+json' }
            } else {
                $release = Get-Content -Raw -Path $api -ErrorAction Stop | ConvertFrom-Json
            }
        } catch {
            return $null
        }
        $tag = "$($release.tag_name)"
        if ($tag -match '^[A-Za-z0-9._-]+$') { return $tag }
        return $null
    }

    # The git ref to install: BETTER_CAPTIONS_REF when set (a branch such as
    # main, a tag or a commit SHA), else the latest release, else main.
    function Get-InstallRef {
        if ($env:BETTER_CAPTIONS_REF) { return $env:BETTER_CAPTIONS_REF }
        $tag = Get-LatestReleaseTag
        if ($tag) {
            Say "Latest release: $tag"
            return $tag
        }
        Warn "could not find the latest release (none published yet, or GitHub's API failed or is rate-limited);"
        Warn 'installing the development version from main. Set BETTER_CAPTIONS_REF to pick a release.'
        return 'main'
    }

    # Returns the source dir to install from: BETTER_CAPTIONS_SOURCE if set,
    # else git ref $Ref downloaded from GitHub into $WorkDir.
    function Get-Source([string]$WorkDir, [string]$Ref) {
        if ($env:BETTER_CAPTIONS_SOURCE) {
            if (-not (Test-Path (Join-Path $env:BETTER_CAPTIONS_SOURCE 'pyproject.toml'))) {
                throw "BETTER_CAPTIONS_SOURCE=$env:BETTER_CAPTIONS_SOURCE has no pyproject.toml"
            }
            return (Resolve-Path $env:BETTER_CAPTIONS_SOURCE).Path
        }
        $url = "https://github.com/$Repo/archive/$Ref.zip"
        $zip = Join-Path $WorkDir 'src.zip'
        Say "Downloading $Repo@$Ref"
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        } catch {
            throw "could not download $url (check BETTER_CAPTIONS_REF and your network): $_"
        }
        $unpacked = Join-Path $WorkDir 'src'
        Expand-Archive -Path $zip -DestinationPath $unpacked -Force
        # GitHub zips contain a single top-level <repo>-<ref> folder.
        $top = Get-ChildItem -Path $unpacked -Directory | Select-Object -First 1
        if (-not $top) { throw "could not unpack $url" }
        return $top.FullName
    }

    function Get-VenvPython([string]$Data) {
        return Join-Path $Data 'venv\Scripts\python.exe'
    }

    function New-EngineVenv([string]$Data) {
        $py = Get-VenvPython $Data
        if (Test-Path $py) {
            & $py -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>$null
            if ($LASTEXITCODE -eq 0) {
                Say "Reusing engine venv at $Data\venv"
                return
            }
        }
        Say "Creating engine venv (Python $PythonVersion) at $Data\venv"
        Invoke-Checked 'venv creation' {
            uv venv --no-project --no-config --clear --managed-python --python $PythonVersion (Join-Path $Data 'venv')
        }
    }

    # -----------------------------------------------------------------------
    # NVIDIA GPU
    # faster-whisper runs on NVIDIA GPUs with the CUDA 12 cuBLAS + cuDNN 9
    # runtime, installed from pip as the package's `cuda` extra. See
    # docs/GPU.md.
    # -----------------------------------------------------------------------

    # True when nvidia-smi (installed with the NVIDIA driver) lists a GPU, or
    # Windows reports an NVIDIA display adapter.
    function Test-NvidiaGpu {
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($smi) {
            try {
                $out = & $smi.Source -L 2>$null
                if ($LASTEXITCODE -eq 0 -and ("$out" -match 'GPU \d+')) { return $true }
            } catch { }
        }
        if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {
            try {
                $adapters = Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop
                if ($adapters | Where-Object { "$($_.Name) $($_.AdapterCompatibility)" -match 'NVIDIA' }) { return $true }
            } catch { }
        }
        return $false
    }

    # 'cuda' or 'none': whether to install the CUDA runtime.
    # BETTER_CAPTIONS_GPU=auto|cuda|none overrides the detection.
    function Get-GpuChoice {
        $choice = if ($env:BETTER_CAPTIONS_GPU) { $env:BETTER_CAPTIONS_GPU } else { 'auto' }
        switch ($choice) {
            'cuda' { return 'cuda' }
            'none' { return 'none' }
            'auto' { if (Test-NvidiaGpu) { return 'cuda' } else { return 'none' } }
        }
        throw "BETTER_CAPTIONS_GPU must be auto, cuda or none (got '$choice')"
    }

    function Install-Package([string]$Data, [string]$Src, [string]$Gpu) {
        $py = Get-VenvPython $Data
        $uvArgs = @('pip', 'install', '--no-config', '--python', $py, '--reinstall-package', 'better-captions')
        if ($env:BETTER_CAPTIONS_SKIP_DEPS -eq '1') { $uvArgs += '--no-deps' }
        $target = $Src
        if ($Gpu -eq 'cuda') {
            $target = $Src + '[cuda]'
            Say 'NVIDIA GPU: also installing its CUDA runtime (cuBLAS + cuDNN, about 1.5 GB)'
        }
        Say 'Installing the Better Captions engine (this can take a few minutes the first time)'
        Invoke-Checked 'package install' { uv @uvArgs $target }
    }

    # Writes config.json, keeping any user settings already in it.
    function Write-Config([string]$Data, [string]$Gpu) {
        $py = Get-VenvPython $Data
        $path = Join-Path $Data 'config.json'
        $version = & $py -c "import importlib.metadata as m; print(m.version('better-captions'))"
        if ($LASTEXITCODE -ne 0) { throw 'could not read the installed engine version' }
        $config = [ordered]@{}
        if (Test-Path $path) {
            try {
                $existing = Get-Content -Raw -Path $path | ConvertFrom-Json
                foreach ($p in $existing.PSObject.Properties) { $config[$p.Name] = $p.Value }
            } catch {
                Warn "ignoring unreadable $path"
            }
        }
        $config['engine'] = @($py, '-m', 'better_captions.cli')
        $config['version'] = "$version".Trim()
        $config['gpu'] = $Gpu
        $json = $config | ConvertTo-Json -Depth 10
        # UTF-8 without BOM, so Python's json module reads it.
        [System.IO.File]::WriteAllText($path, $json + "`n", (New-Object System.Text.UTF8Encoding $false))
    }

    # Copies resolve_plugin\Utility\* into Resolve's Scripts\Utility and records
    # what was copied in <data>\plugin_files.txt so upgrades and uninstall can
    # remove exactly those entries.
    function Install-Plugin([string]$Data, [string]$Src) {
        $dest = Get-ResolveUtilityDir
        $manifest = Join-Path $Data 'plugin_files.txt'
        if (-not (Test-ResolveInstalled)) {
            Warn 'DaVinci Resolve was not found; installing the plugin anyway. Install Resolve to use it.'
        }
        $pluginSrc = Join-Path $Src 'resolve_plugin\Utility'
        if (-not (Test-Path $pluginSrc)) {
            Warn 'no resolve_plugin\Utility in the source; skipping the Resolve plugin'
            return
        }
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        if (Test-Path $manifest) {
            foreach ($old in Get-Content -Path $manifest) {
                if ($old) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $dest $old) }
            }
        }
        $names = @()
        foreach ($entry in Get-ChildItem -Force -Path $pluginSrc) {
            Copy-Item -Recurse -Force -Path $entry.FullName -Destination $dest
            $names += $entry.Name
        }
        Set-Content -Path $manifest -Value $names
        Say "Installed the Resolve plugin into $dest"
    }

    # -----------------------------------------------------------------------
    # RESOLVE SYSTEM PYTHON
    # Resolve runs the plugin with a system Python it finds by itself: a
    # 64-bit python.org CPython 3.10-3.12 with tkinter, installed for all
    # users (C:\Program Files\Python3xx, HKLM\SOFTWARE\Python\PythonCore).
    # Resolve does not detect per-user installs, so this is the one step
    # that needs admin rights. See docs/RESOLVE_INTEGRATION.md
    # ("Installer requirements").
    # -----------------------------------------------------------------------

    # Where Resolve looks for Python, best first. BETTER_CAPTIONS_RESOLVE_PYTHONS
    # (semicolon-separated paths) replaces the list (tests).
    function Get-ResolvePythonCandidates {
        if ($env:BETTER_CAPTIONS_RESOLVE_PYTHONS) {
            return @($env:BETTER_CAPTIONS_RESOLVE_PYTHONS -split ';' | Where-Object { $_ })
        }
        $candidates = @()
        if ($env:RESOLVE_PYTHON3_HOME) { $candidates += Join-Path $env:RESOLVE_PYTHON3_HOME 'python.exe' }
        foreach ($v in '3.12', '3.11', '3.10') {
            $key = "HKLM:\SOFTWARE\Python\PythonCore\$v\InstallPath"
            $installPath = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).'(default)'
            if ($installPath) { $candidates += Join-Path $installPath 'python.exe' }
            $candidates += Join-Path $env:ProgramFiles ('Python' + $v.Replace('.', '') + '\python.exe')
        }
        return $candidates
    }

    # Returns @{Path; Version} for the first candidate that is a 64-bit
    # Python 3.10-3.12 with tkinter, or $null.
    function Find-ResolvePython {
        $probe = "import struct, sys, tkinter; print('%d.%d %d' % (sys.version_info[:2] + (struct.calcsize('P') * 8,)))"
        foreach ($py in Get-ResolvePythonCandidates) {
            if (-not (Test-Path -PathType Leaf $py)) { continue }
            try { $info = & $py -c $probe 2>$null } catch { continue }
            if ($LASTEXITCODE -ne 0 -or -not $info) { continue }
            $version, $bits = "$info".Trim() -split ' '
            if (($version -in @('3.10', '3.11', '3.12')) -and $bits -eq '64') {
                return @{ Path = $py; Version = $version }
            }
        }
        return $null
    }

    # Installs python.org 3.12 for all users, silently. Windows shows one
    # UAC prompt for it.
    function Install-ResolvePython([string]$WorkDir) {
        Say "Resolve needs Python $PythonVersion from python.org, installed for all users, to run the plugin."
        Say 'Installing it now. Windows will ask for administrator permission (UAC): Resolve only finds'
        Say "Python installed for all users under C:\Program Files. Choose Yes to continue."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            & winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { return $true }
            Warn "winget could not install Python (exit code $LASTEXITCODE); trying the python.org installer"
        }
        $url = "https://www.python.org/ftp/python/$ResolvePythonRelease/python-$ResolvePythonRelease-amd64.exe"
        $exe = Join-Path $WorkDir "python-$ResolvePythonRelease-amd64.exe"
        Say "Downloading $url"
        try {
            Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
        } catch {
            Warn "could not download ${url}: $_"
            return $false
        }
        $installerArgs = @('/quiet', 'InstallAllUsers=1', 'Include_tcltk=1', 'PrependPath=0', 'Include_test=0')
        try {
            $proc = Start-Process -FilePath $exe -ArgumentList $installerArgs -Verb RunAs -Wait -PassThru
        } catch {
            Warn "the Python installer did not run (was the UAC prompt declined?): $_"
            return $false
        }
        if ($proc.ExitCode -ne 0) {
            Warn "the Python installer failed (exit code $($proc.ExitCode))"
            return $false
        }
        return $true
    }

    function Ensure-ResolvePython([string]$WorkDir) {
        if ($env:BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON -eq '1') {
            Say 'Skipping the system Python check for Resolve (BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON=1)'
            return
        }
        $found = Find-ResolvePython
        if ($found) {
            Say "Resolve will use Python $($found.Version) at $($found.Path)"
            return
        }
        if (-not (Install-ResolvePython $WorkDir)) {
            Warn "Python was not installed. Install Python $PythonVersion (64-bit) from python.org with"
            Warn '"Install for all users" checked before using the plugin.'
            return
        }
        $found = Find-ResolvePython
        if ($found) {
            Say "Installed Python $($found.Version) at $($found.Path); restart DaVinci Resolve to pick it up"
        } else {
            Warn "Python was installed but Resolve's Python is still not found; see docs/RESOLVE_INTEGRATION.md"
        }
    }

    function Get-InstalledVersion([string]$Data) {
        $version = & (Get-VenvPython $Data) -c "import importlib.metadata as m; print(m.version('better-captions'))"
        if ($LASTEXITCODE -ne 0) { throw 'could not read the installed engine version' }
        return "$version".Trim()
    }

    function Show-NextSteps([string]$Data, [string]$Gpu, [string]$Version, [string]$Origin) {
        if ($Gpu -eq 'cuda') {
            $gpuNote = "The NVIDIA GPU runtime (CUDA) is installed; the engine uses your GPU`n" +
                "automatically and falls back to the CPU if it can't."
        } else {
            $gpuNote = "No NVIDIA GPU was detected, so the CUDA runtime was not installed. To`n" +
                "install it anyway, set `$env:BETTER_CAPTIONS_GPU = 'cuda' and re-run."
        }
        $py = Get-VenvPython $Data
        Write-Host @"

Better Captions $Version is installed (from $Origin).
  Engine:  $Data\venv
  Config:  $Data\config.json

Next steps:
  1. Restart DaVinci Resolve if it is running.
  2. Open Workspace > Scripts > Better Captions.

$gpuNote
Check what the engine detects: & "$py" -m better_captions.cli doctor
Re-run this installer any time to upgrade.
"@
    }

    # Tests: make the functions above callable from the caller's session instead of installing.
    if ($env:BETTER_CAPTIONS_LIB -eq '1') {
        foreach ($f in Get-ChildItem function:) {
            if ($f.ScriptBlock.File -and $f.ScriptBlock.File -eq $PSCommandPath) {
                Set-Item -Path "function:global:$($f.Name)" -Value $f.ScriptBlock
            }
        }
        return
    }

    $workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("better-captions-" + [guid]::NewGuid())
    try {
        $data = Get-DataDir
        New-Item -ItemType Directory -Force -Path $data | Out-Null
        New-Item -ItemType Directory -Force -Path $workDir | Out-Null
        $gpu = Get-GpuChoice
        Ensure-Uv
        if ($env:BETTER_CAPTIONS_SOURCE) {
            $ref = ''
            $origin = $env:BETTER_CAPTIONS_SOURCE
        } else {
            $ref = Get-InstallRef
            $origin = "$Repo@$ref"
        }
        $src = Get-Source $workDir $ref
        New-EngineVenv $data
        Install-Package $data $src $gpu
        Write-Config $data $gpu
        Install-Plugin $data $src
        Ensure-ResolvePython $workDir
        Show-NextSteps $data $gpu (Get-InstalledVersion $data) $origin
    } catch {
        Write-Host "error: $_" -ForegroundColor Red
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $workDir
    }
}
