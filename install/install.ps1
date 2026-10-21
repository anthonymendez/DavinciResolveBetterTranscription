# Better Captions installer for Windows.
#
#   irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.ps1 | iex
#
# Re-running it upgrades an existing install. Never needs admin rights.
# Install layout: see docs/ARCHITECTURE.md ("Install layout").
#
# Environment:
#   BETTER_CAPTIONS_HOME       data dir override (default: %LOCALAPPDATA%\BetterCaptions)
#   BETTER_CAPTIONS_REF        git ref to install from GitHub (default: main)
#   BETTER_CAPTIONS_SOURCE     install from this local source dir instead of GitHub (dev/tests)
#   BETTER_CAPTIONS_SKIP_DEPS  =1: install the package with --no-deps (tests only)
#
# Runs via `iex`, so it never calls `exit` (that would close the user's
# shell): errors are thrown and reported by the try/catch at the bottom.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    $Repo = 'anthonymendez/DavinciResolveBetterTranscription'
    $PythonVersion = '3.12'

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

    # Returns the source dir to install from. Downloads into $WorkDir unless
    # BETTER_CAPTIONS_SOURCE is set.
    function Get-Source([string]$WorkDir) {
        if ($env:BETTER_CAPTIONS_SOURCE) {
            if (-not (Test-Path (Join-Path $env:BETTER_CAPTIONS_SOURCE 'pyproject.toml'))) {
                throw "BETTER_CAPTIONS_SOURCE=$env:BETTER_CAPTIONS_SOURCE has no pyproject.toml"
            }
            return (Resolve-Path $env:BETTER_CAPTIONS_SOURCE).Path
        }
        $ref = if ($env:BETTER_CAPTIONS_REF) { $env:BETTER_CAPTIONS_REF } else { 'main' }
        $url = "https://github.com/$Repo/archive/$ref.zip"
        $zip = Join-Path $WorkDir 'src.zip'
        Say "Downloading $Repo@$ref"
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

    function Install-Package([string]$Data, [string]$Src) {
        $py = Get-VenvPython $Data
        $uvArgs = @('pip', 'install', '--no-config', '--python', $py, '--reinstall-package', 'better-captions')
        if ($env:BETTER_CAPTIONS_SKIP_DEPS -eq '1') { $uvArgs += '--no-deps' }
        Say 'Installing the Better Captions engine (this can take a few minutes the first time)'
        Invoke-Checked 'package install' { uv @uvArgs $Src }
    }

    # Writes config.json, keeping any user settings already in it.
    function Write-Config([string]$Data) {
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
    # RESOLVE SYSTEM PYTHON (placeholder)
    # If the Resolve plugin turns out to need a system Python for Resolve's
    # own scripting host (Workspace > Scripts runs .py scripts with an
    # external Python 3), detect/install it here and point Resolve at it.
    # Currently a no-op: the plugin talks to the engine through
    # config.json["engine"].
    # -----------------------------------------------------------------------
    function Ensure-ResolvePython {
    }

    function Show-NextSteps([string]$Data) {
        Write-Host @"

Better Captions is installed.
  Engine:  $Data\venv
  Config:  $Data\config.json

Next steps:
  1. Restart DaVinci Resolve if it is running.
  2. Open Workspace > Scripts > Better Captions.

An NVIDIA GPU with CUDA is used automatically if available; otherwise the
engine runs on the CPU.
Re-run this installer any time to upgrade.
"@
    }

    $workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("better-captions-" + [guid]::NewGuid())
    try {
        $data = Get-DataDir
        New-Item -ItemType Directory -Force -Path $data | Out-Null
        New-Item -ItemType Directory -Force -Path $workDir | Out-Null
        Ensure-Uv
        $src = Get-Source $workDir
        New-EngineVenv $data
        Install-Package $data $src
        Write-Config $data
        Install-Plugin $data $src
        Ensure-ResolvePython
        Show-NextSteps $data
    } catch {
        Write-Host "error: $_" -ForegroundColor Red
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $workDir
    }
}
