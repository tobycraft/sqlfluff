<#
.SYNOPSIS
    Walks every commit since a base commit, building the Rust extension and
    running the full test suite at each one, logging everything to a file.

.DESCRIPTION
    For each commit in (BaseCommit..original HEAD], oldest first:
      1. git checkout <commit>
      2. uv pip install -e ./sqlfluffrs/   (rebuild + install the Rust extension)
      3. uv run pytest test/               (run the full suite)
    stdout/stderr from every step is appended to -LogFile, tagged with the
    commit hash/subject and a pass/fail marker. A build or test failure does
    not stop the walk - it's logged and the script moves to the next commit.
    The original branch/commit is checked back out when the walk finishes.

    Requires `uv` on PATH. Run this with an activated Python venv that
    already has `uv pip install -e .` done for the main package - this
    script only rebuilds the Rust extension per commit, not the Python
    package itself.

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

foreach ($Commit in $Commits) {
    $Subject = git log -1 --format=%s $Commit
    $Header = "`n===== $Commit $Subject ($(Get-Date -Format o)) ====="
    Write-Host $Header
    Add-Content -Path $LogFile -Value $Header

    git checkout --force $Commit *>> $LogFile

    $BuildOk = $true
    uv pip install -e ./sqlfluffrs/ *>> $LogFile
    if ($LASTEXITCODE -ne 0) {
        $BuildOk = $false
        Add-Content -Path $LogFile -Value "--- BUILD FAILED (exit $LASTEXITCODE), skipping tests for $Commit ---"
    }

    if ($BuildOk) {
        uv run pytest test/ *>> $LogFile
        $TestExit = $LASTEXITCODE
        Add-Content -Path $LogFile -Value "--- RESULT for $Commit`: pytest exit $TestExit ---"
    }
}

git checkout --force $OriginalRef *>> $LogFile
"=== Bisect run finished $(Get-Date -Format o) ===" | Add-Content -Path $LogFile
Pop-Location
