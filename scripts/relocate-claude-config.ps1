<#
.SYNOPSIS
    Relocates Claude Code's per-user data directory (~/.claude — session
    history/transcripts, memory, settings, credentials, plugins) from the
    Windows profile onto this repo's D:\QuickJoiner\.claude-home, then
    replaces ~/.claude with a directory junction pointing there.

.WHY
    On a machine whose Windows profile (C:\Users\<you>) gets periodically
    wiped by org policy/imaging, ~/.claude and everything under it
    (including conversation history) gets wiped with it. D:\QuickJoiner is
    a separate, persistent volume. A junction makes every Claude Code
    client (VS Code extension or CLI) keep reading/writing the ordinary
    ~/.claude path while the OS transparently redirects it to D: — this
    works around a known bug where the VS Code extension ignores the
    CLAUDE_CONFIG_DIR environment variable (anthropics/claude-code#30538).

.IMPORTANT
    Run this with VS Code and every `claude` terminal session FULLY CLOSED.
    Running it while a session is active will not corrupt anything
    irreversibly, but the live session will just recreate ~/.claude under
    your feet mid-run (this is exactly what happened when it was first
    attempted live) and you'll end up with yet another .claude.bak-* to
    merge on the next run. It's idempotent — safe to re-run.

.WHAT IT DOES
    1. Finds every C:\Users\<you>\.claude.bak-* left over from a prior
       attempt, plus the live ~/.claude (if it's a real directory, not
       already a junction).
    2. For any *.jsonl session transcript that got split across two of
       these (the exact failure mode above), concatenates them back into
       one file in chronological order (oldest backup first, live last).
    3. Copies everything else into D:\QuickJoiner\.claude-home, live
       taking priority over older backups for anything overlapping.
    4. Renames the live ~/.claude aside one last time and replaces it
       with a junction to D:\QuickJoiner\.claude-home.

    Nothing is deleted. Old backups are left behind (as
    .claude.bak-final and any earlier .claude.bak-* dirs) for you to
    remove by hand once you've confirmed everything looks right.
#>

$ErrorActionPreference = "Stop"
$live   = "$env:USERPROFILE\.claude"
$target = "D:\QuickJoiner\.claude-home"

$existing = Get-Item $live -ErrorAction SilentlyContinue
if ($existing -and $existing.LinkType -eq "Junction") {
    Write-Output "$live is already a junction -> $($existing.Target). Nothing to do."
    return
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

# Any prior partial-relocation backups, oldest name first (lexical sort is
# fine since they're named .claude.bak-<date>).
$backups = Get-ChildItem "$env:USERPROFILE" -Directory -Filter ".claude.bak-*" -ErrorAction SilentlyContinue |
    Sort-Object Name

# --- Step 1: merge any session transcripts split across a backup + the live dir ---
foreach ($bak in $backups) {
    $bakProjects = Join-Path $bak.FullName "projects"
    if (-not (Test-Path $bakProjects)) { continue }
    Get-ChildItem -Recurse -File -Filter *.jsonl $bakProjects | ForEach-Object {
        $rel       = $_.FullName.Substring($bak.FullName.Length + 1)
        $livePeer  = Join-Path $live $rel
        $destPath  = Join-Path $target $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $destPath) | Out-Null
        if ((Test-Path $livePeer) -and $existing -and $existing.LinkType -ne "Junction") {
            Get-Content $_.FullName, $livePeer | Set-Content $destPath
            Write-Output "Merged split transcript: $rel"
        } elseif (-not (Test-Path $destPath)) {
            Copy-Item $_.FullName $destPath -Force
        }
    }
}

# --- Step 2: baseline copy everything else — oldest backups first, live last (wins) ---
foreach ($bak in $backups) {
    robocopy $bak.FullName $target /E /R:1 /W:1 /NFL /NDL /NJH /XF *.jsonl | Out-Null
}
if ($existing -and $existing.LinkType -ne "Junction") {
    robocopy $live $target /E /R:1 /W:1 /NFL /NDL /NJH /XF *.jsonl | Out-Null
}

# --- Step 3: swap the live directory for a junction ---
if ($existing -and $existing.LinkType -ne "Junction") {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Rename-Item -Path $live -NewName ".claude.bak-final-$stamp" -ErrorAction Stop
}
New-Item -ItemType Junction -Path $live -Target $target | Out-Null

Write-Output ""
Write-Output "Done: $live now points at $target"
Write-Output "Verify things look right, then you can delete any C:\Users\$env:USERNAME\.claude.bak-* folders."
