<#
.SYNOPSIS
    Walks every commit since a base commit, building the Rust extension and
    running the full test suite at each one, logging everything to a file.

.DESCRIPTION
    Once, up front: uv pip install -e .   (main package, into the venv below)
    Then for each commit in (BaseCommit..original HEAD], oldest first:
      1. git checkout <commit>
      2. uv pip install -e ./sqlfluffrs/   (rebuild + install the Rust extension)
      3. python -m pytest test/            (run the full suite)
    Every install/test step targets the same venv explicitly by path (see
    below) rather than trusting ambient PATH/`uv run` resolution.
    stdout/stderr from every step is appended to -LogFile, tagged with the
    commit hash/subject and a pass/fail marker. A build or test failure does
    not stop the walk - it's logged and the script moves to the next commit.
    The original branch/commit is checked back out when the walk finishes.

    Requires `uv` on PATH and $env:VIRTUAL_ENV pointing at an activated
    Python venv. Every step below explicitly targets that venv's python.exe
    by path (via `uv pip install --python` and by invoking it directly for
    pytest) rather than relying on bare `python`/`uv run` to "discover" the
    right environment on their own - on Windows in particular, activating a
    venv can set $env:VIRTUAL_ENV (which uv respects) without necessarily
    putting that venv's Scripts dir first on PATH (which bare `python`
    relies on), so `uv pip install` and a bare `python -m pytest` can
    silently land in two different interpreters, and `uv run` resolves its
    own project-managed .venv independently of either.

.PARAMETER BaseCommit
    Commit to start after (exclusive). Defaults to ab292785.

.PARAMETER LogFile
    Path to the combined log file. Defaults to bisect-history.log in the
    current directory.
#>
param(
    [string]$BaseCommit = "ab292785",
    [string]$LogFile = "bisect-history.log"
)

$ErrorActionPreference = "Continue"
$RepoRoot = git rev-parse --show-toplevel
Push-Location $RepoRoot

if (-not $env:VIRTUAL_ENV) {
    Write-Error "`$env:VIRTUAL_ENV is not set - activate the venv you want this to use before running this script."
    Pop-Location
    exit 1
}

# Resolve the venv's python.exe explicitly, by path, rather than trusting
# bare `python` on PATH to resolve to the same venv uv targets. See the
# comment block above for why that trust would be misplaced here.
$PythonExe = if ($IsWindows -or $env:OS -eq "Windows_NT") {
    Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
} else {
    Join-Path $env:VIRTUAL_ENV "bin/python"
}
if (-not (Test-Path $PythonExe)) {
    Write-Error "Could not find python at '$PythonExe' under `$env:VIRTUAL_ENV ($env:VIRTUAL_ENV)."
    Pop-Location
    exit 1
}

$OriginalRef = git rev-parse --abbrev-ref HEAD
if ($OriginalRef -eq "HEAD") { $OriginalRef = git rev-parse HEAD }

$Commits = @(git rev-list --reverse "$BaseCommit..$OriginalRef")
if (-not $Commits) {
    Write-Error "No commits found in range $BaseCommit..$OriginalRef"
    Pop-Location
    exit 1
}

"=== Bisect run started $(Get-Date -Format o) : $($Commits.Count) commits, base=$BaseCommit, ref=$OriginalRef ===" |
    Tee-Object -FilePath $LogFile -Append | Out-Null

Add-Content -Path $LogFile -Value "`n--- one-time setup: uv pip install --python $PythonExe -e . ---"
uv pip install --python $PythonExe -e . *>> $LogFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "Initial 'uv pip install -e .' failed (exit $LASTEXITCODE) - see $LogFile"
    Pop-Location
    exit 1
}

foreach ($Commit in $Commits) {
    $Subject = git log -1 --format=%s $Commit
    $Header = "`n===== $Commit $Subject ($(Get-Date -Format o)) ====="
    Write-Host $Header
    Add-Content -Path $LogFile -Value $Header

    git checkout --force $Commit *>> $LogFile

    $BuildOk = $true
    uv pip install --python $PythonExe -e ./sqlfluffrs/ *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        $BuildOk = $false
        Add-Content -Path $LogFile -Value "--- BUILD FAILED (exit $LASTEXITCODE), skipping tests for $Commit ---"
    }

    if ($BuildOk) {
        & $PythonExe -m pytest test/ *>> $LogFile
        $TestExit = $LASTEXITCODE
        Add-Content -Path $LogFile -Value "--- RESULT for $Commit`: pytest exit $TestExit ---"
    }
}

git checkout --force $OriginalRef *>> $LogFile
"=== Bisect run finished $(Get-Date -Format o) ===" | Add-Content -Path $LogFile
Pop-Location
