# Better Captions installer with the plugin's debug log turned on, for Windows.
#
#   irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install-debug.ps1 | iex
#
# Runs install.ps1 with BETTER_CAPTIONS_DEBUG=1; every other BETTER_CAPTIONS_* variable
# (BETTER_CAPTIONS_SOURCE, BETTER_CAPTIONS_REF, ...) is passed through. Each run in Resolve
# then writes %LOCALAPPDATA%\BetterCaptions\logs\better-captions-*.log. Re-run the normal
# install.ps1 to turn the log off again.
#
# Runs via `iex`, so it never calls `exit` (that would close the user's shell).

& {
    $previous = $env:BETTER_CAPTIONS_DEBUG
    $env:BETTER_CAPTIONS_DEBUG = '1'
    try {
        # Run from a local copy: use the install.ps1 next to this file.
        $local = if ($PSCommandPath) { Join-Path (Split-Path -Parent $PSCommandPath) 'install.ps1' } else { $null }
        if ($local -and (Test-Path $local)) {
            & $local @args
        } else {
            Invoke-RestMethod 'https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.ps1' |
                Invoke-Expression
        }
    } catch {
        Write-Host "error: $_" -ForegroundColor Red
    } finally {
        # Don't leave debug on for a later, normal install from the same PowerShell window.
        $env:BETTER_CAPTIONS_DEBUG = $previous
    }
} @args
