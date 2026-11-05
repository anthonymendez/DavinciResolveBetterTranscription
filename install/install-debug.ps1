# Better Captions installer with the plugin's debug log turned on, for Windows.
#
#   irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install-debug.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File .\install\install-debug.ps1   (in a clone or extracted zip: installs that copy)
#
# Options, so no environment variable has to be set by hand (any BETTER_CAPTIONS_* variable that is
# set still applies; an option wins over it):
#   -Ref REF       install this branch, tag or commit from GitHub (e.g. -Ref main)
#   -Source DIR    install from this local copy (the folder with pyproject.toml)
#   -GitHub        install from GitHub even when run from a local copy
# With none of them, a run from a local copy installs that copy, and `irm | iex` installs the latest
# release from GitHub. Options with the one-line install:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install-debug.ps1))) -Ref main
# Each run in Resolve then writes %LOCALAPPDATA%\BetterCaptions\logs\better-captions-*.log. Re-run the
# normal install.ps1 to turn the log off again. The variables it sets are put back afterwards, so a
# later normal install from the same window is not affected.
#
# Runs via `iex`, so it never calls `exit` (that would close the user's shell).

& {
    $names = 'BETTER_CAPTIONS_DEBUG', 'BETTER_CAPTIONS_SOURCE', 'BETTER_CAPTIONS_REF'
    $previous = @{}
    foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        $ref = $null
        $source = $null
        $github = $false
        for ($i = 0; $i -lt $args.Count; $i++) {
            $option = [string]$args[$i]
            switch -Regex ($option) {
                '^--?ref$' {
                    if ($i + 1 -ge $args.Count) { throw '-Ref needs a branch, tag or commit' }
                    $ref = [string]$args[++$i]
                }
                '^--?source$' {
                    if ($i + 1 -ge $args.Count) { throw '-Source needs a folder' }
                    $source = [string]$args[++$i]
                }
                '^--?github$' { $github = $true }
                default { throw "unknown option: $option (use -Ref REF, -Source DIR or -GitHub)" }
            }
        }
        if ($source -and ($ref -or $github)) {
            throw "-Source installs a local copy; it can't be combined with -Ref or -GitHub"
        }

        # Run from a file: the install.ps1 next to it, and the copy it sits in (if it is one).
        $here = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { $null }
        $root = if ($here -and (Test-Path (Join-Path $here '..\pyproject.toml'))) {
            (Resolve-Path (Join-Path $here '..')).Path
        } else { $null }

        $env:BETTER_CAPTIONS_DEBUG = '1'
        if ($source) {
            $env:BETTER_CAPTIONS_SOURCE = $source
        } elseif ($ref -or $github) {
            $env:BETTER_CAPTIONS_SOURCE = $null
        } elseif (-not $env:BETTER_CAPTIONS_SOURCE -and $root) {
            $env:BETTER_CAPTIONS_SOURCE = $root
        }
        if ($ref) { $env:BETTER_CAPTIONS_REF = $ref }

        $local = if ($here) { Join-Path $here 'install.ps1' } else { $null }
        if ($local -and (Test-Path $local)) {
            & $local
        } else {
            Invoke-RestMethod 'https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.ps1' |
                Invoke-Expression
        }
    } catch {
        Write-Host "error: $_" -ForegroundColor Red
    } finally {
        # Don't leave debug (or this run's source and ref) on for a later install from the same window.
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    }
} @args
