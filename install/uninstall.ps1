# Better Captions uninstaller for Windows.
#
#   irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/uninstall.ps1 | iex
#
# Removes the engine venv, config.json and the Resolve plugin files the
# installer copied. Leaves uv installed. Honours BETTER_CAPTIONS_HOME.

& {
    $ErrorActionPreference = 'Stop'

    function Say([string]$Message) { Write-Host "==> $Message" }

    try {
        $data = if ($env:BETTER_CAPTIONS_HOME) { $env:BETTER_CAPTIONS_HOME } else { Join-Path $env:LOCALAPPDATA 'BetterCaptions' }
        $dest = Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
        $manifest = Join-Path $data 'plugin_files.txt'

        if (Test-Path $manifest) {
            foreach ($entry in Get-Content -Path $manifest) {
                if ($entry) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $dest $entry) }
            }
            Say "Removed the Resolve plugin from $dest"
        }

        if (-not (Test-Path $data)) {
            Say "Nothing installed at $data"
            return
        }
        # Only remove what the installer created: BETTER_CAPTIONS_HOME may
        # point at a directory that holds other things.
        foreach ($name in 'venv', 'config.json', 'plugin_files.txt') {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $data $name)
        }
        if (-not (Get-ChildItem -Force -Path $data)) {
            Remove-Item -Force $data
        } else {
            Say "Left $data in place (it still contains other files)"
        }
        Say 'Better Captions has been uninstalled.'
    } catch {
        Write-Host "error: $_" -ForegroundColor Red
    }
}
