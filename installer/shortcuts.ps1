<#
.SYNOPSIS
    LeRoy - shortcuts.ps1  (creates the single "Leroy CLI" desktop shortcut)

.DESCRIPTION
    Creates ONE Desktop shortcut:

      "Leroy CLI"  -> opens a terminal whose working directory is the user's
                      ~\.claude folder and starts Claude Code (`claude`) there,
                      so Claude Code loads the LeRoy config/agents/skills for
                      this account automatically.

    There is deliberately no second "Leroy" (UI) shortcut: this build is
    CLI-first and ships no desktop app, so a UI shortcut would only point at a
    "coming soon" notice. If an old "Leroy.lnk" from a previous install is on
    the Desktop, we remove it so the user is left with exactly one shortcut.

    Desktop resolution uses the Windows known-folder API
    ([Environment]::GetFolderPath('DesktopDirectory')), NOT $HOME\Desktop. On
    accounts where OneDrive has taken over the Desktop (Known Folder Move),
    $HOME\Desktop is a stale/empty folder and the *visible* Desktop lives under
    OneDrive - writing the .lnk to $HOME\Desktop is why an earlier build
    reported success but the icon never appeared.

    All paths are bound PowerShell parameters (never string interpolation) so
    apostrophes/spaces in a username or path can't break the generated target.

    Idempotent: re-running overwrites the .lnk cleanly.

.PARAMETER ClaudeHome
    The user's ~\.claude directory (defaults to $HOME\.claude).

.PARAMETER RepoRoot
    Accepted for backward compatibility with setup.ps1's call; unused.

.PARAMETER DesktopDir
    Override the Desktop folder (used by sandbox tests so they don't write onto
    the real operator Desktop). When omitted, the real per-user Desktop is
    resolved via the known-folder API.

.EXAMPLE
    powershell -File installer\shortcuts.ps1
    powershell -File installer\shortcuts.ps1 -DesktopDir "C:\temp\fake-desktop" -DryRun
#>

[CmdletBinding()]
param(
    [string]$ClaudeHome  = (Join-Path $HOME ".claude"),
    [string]$RepoRoot    = "",
    [string]$DesktopDir  = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Say($t) { Write-Host "  $t" }

# --- resolve the REAL Desktop (honors OneDrive Known Folder Move) ------------
if (-not $DesktopDir) {
    try {
        $DesktopDir = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    } catch {
        $DesktopDir = ""
    }
    if (-not $DesktopDir) { $DesktopDir = Join-Path $HOME "Desktop" }
}

if (-not (Test-Path $DesktopDir)) {
    try { New-Item -ItemType Directory -Force -Path $DesktopDir | Out-Null } catch {}
}
if (-not (Test-Path $DesktopDir)) {
    Say "Desktop folder not found at $DesktopDir - skipping shortcut creation."
    Say "You can still launch LeRoy any time: open a terminal in $ClaudeHome and type 'leroy'."
    exit 0
}

$cliShortcut = Join-Path $DesktopDir "Leroy CLI.lnk"
$staleUi     = Join-Path $DesktopDir "Leroy.lnk"

if ($DryRun) {
    Say "[dry-run] Desktop resolved to: $DesktopDir"
    Say "[dry-run] would create: $cliShortcut  (terminal at $ClaudeHome -> starts Claude Code)"
    if (Test-Path $staleUi) { Say "[dry-run] would remove stale UI shortcut: $staleUi" }
    exit 0
}

# --- remove a stale "Leroy" (UI) shortcut from a previous install ------------
if (Test-Path $staleUi) {
    try {
        Remove-Item -Path $staleUi -Force
        Say "Removed old 'Leroy' shortcut (this build has no desktop app - CLI only)."
    } catch {
        Say "Note: could not remove old 'Leroy' shortcut at $staleUi (in use?) - safe to delete by hand."
    }
}

# --- "Leroy CLI" - terminal at ~/.claude, starts Claude Code -----------------
# WorkingDirectory sets the folder Claude Code opens in, so it loads the LeRoy
# config for this account. -NoExit keeps the window open (and shows any error
# if `claude` isn't on PATH yet) instead of flashing closed.
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$W = New-Object -ComObject WScript.Shell
$cli = $W.CreateShortcut($cliShortcut)
$cli.TargetPath       = $psExe
$cli.Arguments        = "-NoExit -NoLogo -Command claude"
$cli.WorkingDirectory = $ClaudeHome
$cli.IconLocation     = "$psExe,0"
$cli.Description      = "Open a terminal in ~/.claude and start Claude Code (LeRoy)"
$cli.Save()

Say "Created 'Leroy CLI' shortcut on your Desktop: $cliShortcut"
Say "It opens a terminal in $ClaudeHome and starts Claude Code there."
