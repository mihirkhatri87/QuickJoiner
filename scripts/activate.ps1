<#
.SYNOPSIS
    One command to get a working QuickJoiner dev shell on ANY Windows machine:
    activates (creating if needed) the Python venv, and puts a Node the
    frontend can actually build with on PATH - installing one if the machine
    has none.

.WHY
    `python -m venv` regenerates `.venv\Scripts\Activate.ps1` on every venv
    rebuild, so a hand-edit there does not survive a clone. This wrapper is
    checked into the repo, so `git clone` + this script is the whole setup
    story regardless of what is or is not already installed.

    The Node half exists because "node is installed" is not the same as
    "node works here". nvm-windows can have several versions installed with
    an OLD one globally selected (`C:\Program Files\nodejs` is nvm's symlink
    target), and Vite 5 needs Node 18+ - under Node 16 the build dies with
    the un-obvious "crypto$2.getRandomValues is not a function". Rather than
    changing the machine's GLOBAL Node (which would affect every other
    project on it), this puts a suitable Node first on PATH for this session
    only.

.RESOLUTION ORDER (first hit wins - cheapest and least invasive first)
    1. A repo-local Node previously provisioned by this script (.tools\node).
    2. The pinned version, if nvm-windows already has it installed.
    3. Any other nvm-installed version meeting the minimum (highest first).
    4. Whatever `node` is already on PATH, if it meets the minimum.
    5. Provision the pinned version: via `nvm install` when nvm is present,
       otherwise by downloading the official Node zip from nodejs.org into
       .tools\node (no admin rights, no installer, nothing touched outside
       this repo). The download is CHECKSUM-VERIFIED against nodejs.org's
       own SHASUMS256.txt before anything is extracted or executed.

    Steps 3 and 4 deliberately accept a version that is not the pin: a
    machine that already has a working Node 20 should not be made to
    download another one. The script says when it does this, so a
    version-specific problem is still diagnosable.

.USAGE
        .\scripts\activate.ps1                   # from the repo root
        & D:\QuickJoiner\scripts\activate.ps1    # from anywhere

    Flags:
        -Yes           Never prompt (for CI / unattended use).
        -NoProvision   Never install anything; report and carry on instead.
                       Use when offline or on a locked-down box.
        -SkipPython    Only sort out Node, leave the venv alone.
        -SkipNode      Only sort out the venv, leave Node alone.

    `deactivate` (from the venv's own Activate.ps1) reverts BOTH the venv's
    PATH entry and the Node one added here, because the Node prepend happens
    after the venv script has snapshotted the pre-activation PATH into
    $env:_OLD_VIRTUAL_PATH.

.ENCODING
    Keep this file pure ASCII. Windows PowerShell 5.1 reads a BOM-less script
    in the system ANSI codepage, so a UTF-8 em dash arrives as three cp1252
    characters ending in a smart double quote - which PowerShell accepts as a
    string delimiter, producing parse errors pointing at unrelated lines.

.MAINTENANCE
    Keep $RequiredNodeVersion / $MinNodeMajor in step with what the frontend
    actually needs (Vite's own floor is Node 18), and with the version named
    in CLAUDE.md's dev-environment section.
#>

[CmdletBinding()]
Param(
    [switch]$Yes,
    [switch]$NoProvision,
    [switch]$SkipPython,
    [switch]$SkipNode
)

$ErrorActionPreference = "Stop"

# The version we install when we have to install one. Any already-present
# Node at or above $MinNodeMajor is accepted instead (see resolution order).
$RequiredNodeVersion = "22.22.3"
$MinNodeMajor        = 18                # Vite 5 requires ^18 || >=20
$MinPythonVersion    = [version]"3.11"   # pyproject.toml: requires-python

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$ToolsDir   = Join-Path $RepoRoot ".tools"
$LocalNodes = Join-Path $ToolsDir "node"

function Write-Step  ($m) { Write-Host "  $m" }
function Write-Good  ($m) { Write-Host "  $m" -ForegroundColor Green }
function Write-Note  ($m) { Write-Host "  $m" -ForegroundColor DarkGray }
function Write-Warn2 ($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

function Confirm-Action($Message) {
    if ($Yes) { return $true }
    if (-not [Environment]::UserInteractive) { return $true }
    $answer = Read-Host "  $Message [Y/n]"
    return ($answer -eq "" -or $answer -match '^(y|yes)$')
}

# Version strings arrive as "v22.22.3" or "22.22.3" and occasionally with
# trailing noise; parse leniently and return $null rather than throwing, so a
# weird entry in a version list can never take the whole script down.
function ConvertTo-NodeVersion($Text) {
    if (-not $Text) { return $null }
    if ("$Text" -match '(\d+)\.(\d+)\.(\d+)') {
        try { return [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])" } catch { return $null }
    }
    return $null
}

# ---------------------------------------------------------------- Python ---

function Find-SuitablePython {
    # The `py` launcher knows about every installed CPython and is the only
    # reliable way to pick a version on Windows, where a bare `python` can be
    # a Store stub or an ancient 32-bit build (this repo's own machine has
    # 3.7/3.8 32-bit as the system python, never usable here).
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $listed = & py --list-paths 2>$null
            foreach ($line in $listed) {
                if ("$line" -match '-V:(\d+\.\d+)[^\s]*\s+\*?\s*(.+\.exe)') {
                    $candidates += [pscustomobject]@{
                        Version = [version]$Matches[1]
                        Path    = $Matches[2].Trim()
                    }
                }
            }
        } catch { }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                $raw = & $cmd.Source --version 2>&1
                if ("$raw" -match '(\d+)\.(\d+)\.(\d+)') {
                    $candidates += [pscustomobject]@{
                        Version = [version]"$($Matches[1]).$($Matches[2])"
                        Path    = $cmd.Source
                    }
                }
            } catch { }
        }
    }
    return $candidates |
        Where-Object { $_.Version -ge $MinPythonVersion -and (Test-Path $_.Path) } |
        Sort-Object Version -Descending |
        Select-Object -First 1
}

function Initialize-Venv($VenvActivate) {
    if (Test-Path $VenvActivate) { return $true }

    Write-Warn2 "No venv at $RepoRoot\.venv"
    if ($NoProvision) {
        Write-Note "-NoProvision set; create it with: python -m venv .venv"
        return $false
    }

    $py = Find-SuitablePython
    if (-not $py) {
        Write-Warn2 "No Python $MinPythonVersion+ found. Install one from https://www.python.org/downloads/ and re-run."
        return $false
    }
    if (-not (Confirm-Action "Create .venv using Python $($py.Version) ($($py.Path)) and install dependencies?")) {
        return $false
    }

    Write-Step "Creating venv with Python $($py.Version) ..."
    & $py.Path -m venv (Join-Path $RepoRoot ".venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvActivate)) {
        Write-Warn2 "venv creation failed."
        return $false
    }

    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    Write-Step "Installing dependencies (this takes a few minutes) ..."
    if (Test-Path $uv) {
        & $uv pip install -e "$RepoRoot[dev,browser]" --python $venvPython
    } else {
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -e "$RepoRoot[dev,browser]"
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "Dependency install reported an error - the venv exists, so you can retry the install by hand."
    }
    return $true
}

# ------------------------------------------------------------------ Node ---

function Get-NvmRoot {
    # Ask nvm itself when it is on PATH ('nvm root' prints 'Current Root: <p>').
    if (Get-Command nvm -ErrorAction SilentlyContinue) {
        try {
            $out = (& nvm root 2>$null) -join "`n"
            if ("$out" -match 'Current Root:\s*(.+)') {
                $p = $Matches[1].Trim()
                if (Test-Path $p) { return $p }
            }
        } catch { }
    }
    # nvm can be installed while absent from THIS shell's PATH (it is added by
    # its installer to the machine PATH, which an already-open or stripped-down
    # shell will not have). Its versions are still perfectly usable, so look in
    # the conventional locations rather than concluding there is no nvm.
    foreach ($p in @($env:NVM_HOME, (Join-Path $env:APPDATA "nvm"), "$env:ProgramFiles\nvm")) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

# Every nvm-installed version, newest first. nvm stores each as <root>\v<ver>.
function Get-NvmVersions($NvmRoot) {
    if (-not $NvmRoot -or -not (Test-Path $NvmRoot)) { return @() }
    return Get-ChildItem $NvmRoot -Directory -Filter "v*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $v = ConvertTo-NodeVersion $_.Name
            if ($v -and (Test-Path (Join-Path $_.FullName "node.exe"))) {
                [pscustomobject]@{ Version = $v; Path = $_.FullName }
            }
        } | Sort-Object Version -Descending
}

function Get-LocalNodeDirs {
    if (-not (Test-Path $LocalNodes)) { return @() }
    return Get-ChildItem $LocalNodes -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            $v = ConvertTo-NodeVersion $_.Name
            if ($v -and (Test-Path (Join-Path $_.FullName "node.exe"))) {
                [pscustomobject]@{ Version = $v; Path = $_.FullName }
            }
        } | Sort-Object Version -Descending
}

function Get-PathNode {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    try {
        $v = ConvertTo-NodeVersion (& $cmd.Source --version 2>&1)
        if ($v) { return [pscustomobject]@{ Version = $v; Path = (Split-Path $cmd.Source) } }
    } catch { }
    return $null
}

function Install-NodeViaNvm {
    Write-Step "Installing Node $RequiredNodeVersion via nvm ..."
    # 'nvm install' also switches the machine-global selection, so put it back
    # afterwards: this script's whole contract is that it does not change what
    # other projects on this machine see.
    $before = $null
    try { $before = ConvertTo-NodeVersion ((& nvm current 2>$null) -join "") } catch { }

    & nvm install $RequiredNodeVersion | Out-Host

    if ($before) {
        try { & nvm use "$before" | Out-Null } catch { }
    }
}

function Install-NodePortable {
    $isArm = ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64")
    $arch = if ($isArm) { "arm64" } else { "x64" }
    $name = "node-v$RequiredNodeVersion-win-$arch"
    $zip  = "$name.zip"
    $base = "https://nodejs.org/dist/v$RequiredNodeVersion"
    $dest = Join-Path $LocalNodes $RequiredNodeVersion

    New-Item -ItemType Directory -Force -Path $LocalNodes | Out-Null
    $tmpZip = Join-Path $ToolsDir $zip

    # PS 5.1 defaults can predate TLS 1.2, and its progress bar makes
    # Invoke-WebRequest downloads an order of magnitude slower.
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
    $oldProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Write-Step "Downloading $base/$zip ..."
        Invoke-WebRequest -Uri "$base/$zip" -OutFile $tmpZip -UseBasicParsing

        Write-Step "Verifying checksum against nodejs.org SHASUMS256.txt ..."
        $sums = (Invoke-WebRequest -Uri "$base/SHASUMS256.txt" -UseBasicParsing).Content
        $escaped = [regex]::Escape($zip)
        $pattern = '^([0-9a-fA-F]{64})\s+\*?' + $escaped + '\s*$'
        $expected = $null
        foreach ($line in ("$sums" -split "`n")) {
            if ($line.Trim() -match $pattern) { $expected = $Matches[1].ToLower(); break }
        }
        if (-not $expected) { throw "No checksum published for $zip - refusing to use the download." }

        $actual = (Get-FileHash $tmpZip -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected) {
            throw "Checksum mismatch for $zip (expected $expected, got $actual) - refusing to use the download."
        }
        Write-Good "Checksum OK"

        Write-Step "Extracting ..."
        $staging = Join-Path $ToolsDir "node-staging"
        if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
        Expand-Archive -Path $tmpZip -DestinationPath $staging -Force

        # The archive contains a single top-level <name>\ folder; hoist it to a
        # version-named directory so Get-LocalNodeDirs can parse the version.
        $inner = Join-Path $staging $name
        if (-not (Test-Path $inner)) {
            $inner = (Get-ChildItem $staging -Directory | Select-Object -First 1).FullName
        }
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Move-Item $inner $dest
        Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
        Write-Good "Node $RequiredNodeVersion installed to .tools\node (repo-local, gitignored)"
    }
    finally {
        $ProgressPreference = $oldProgress
        Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-NodeDir {
    # 1. Repo-local, previously provisioned by this script.
    $local = Get-LocalNodeDirs | Where-Object { $_.Version.Major -ge $MinNodeMajor } | Select-Object -First 1
    if ($local) { return @{ Path = $local.Path; Why = "repo-local .tools\node" } }

    $nvmRoot = Get-NvmRoot
    $nvmVersions = Get-NvmVersions $nvmRoot

    # 2. The pinned version via nvm.
    $pinned = ConvertTo-NodeVersion $RequiredNodeVersion
    $exact = $nvmVersions | Where-Object { $_.Version -eq $pinned } | Select-Object -First 1
    if ($exact) { return @{ Path = $exact.Path; Why = "nvm" } }

    # 3. Any other nvm version that is new enough.
    $anyNvm = $nvmVersions | Where-Object { $_.Version.Major -ge $MinNodeMajor } | Select-Object -First 1
    if ($anyNvm) { return @{ Path = $anyNvm.Path; Why = "nvm, not the pinned $RequiredNodeVersion but meets the $MinNodeMajor+ floor" } }

    # 4. Whatever is already on PATH, if usable - nothing to change.
    $onPath = Get-PathNode
    if ($onPath -and $onPath.Version.Major -ge $MinNodeMajor) {
        return @{ Path = $null; Why = "already on PATH" }
    }

    # 5. Nothing usable - provision.
    if ($NoProvision) { return @{ Path = $null; Why = $null; Missing = $true } }

    if ($onPath) {
        Write-Warn2 "Node v$($onPath.Version) is on PATH but the frontend needs $MinNodeMajor+ (Vite fails on older with 'crypto`$2.getRandomValues is not a function')."
    } else {
        Write-Warn2 "No Node found on this machine."
    }

    if ($nvmRoot) {
        if (Confirm-Action "Install Node $RequiredNodeVersion via nvm (leaves your global nvm selection unchanged)?") {
            Install-NodeViaNvm
            $now = Get-NvmVersions $nvmRoot | Where-Object { $_.Version -eq $pinned } | Select-Object -First 1
            if ($now) { return @{ Path = $now.Path; Why = "nvm, just installed" } }
            Write-Warn2 "nvm install did not produce $RequiredNodeVersion; falling back to a repo-local copy."
        } else {
            return @{ Path = $null; Why = $null; Missing = $true }
        }
    }

    if (Confirm-Action "Download Node $RequiredNodeVersion from nodejs.org into .tools\node (repo-local, checksum-verified, no admin rights needed)?") {
        Install-NodePortable
        $fresh = Get-LocalNodeDirs | Select-Object -First 1
        if ($fresh) { return @{ Path = $fresh.Path; Why = "repo-local .tools\node, just installed" } }
    }
    return @{ Path = $null; Why = $null; Missing = $true }
}

# ------------------------------------------------------------------ Main ---

if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
    Write-Warn2 "This script is Windows-only. On macOS/Linux use: source .venv/bin/activate  (and nvm/fnm/asdf for Node $MinNodeMajor+)."
    return
}

Write-Host ""
Write-Host "QuickJoiner dev environment" -ForegroundColor Cyan

if (-not $SkipPython) {
    $VenvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
    if (Initialize-Venv $VenvActivate) {
        & $VenvActivate
        Write-Good "python  $(& python --version 2>&1)"
    } else {
        Write-Warn2 "Continuing without the venv activated."
    }
}

if (-not $SkipNode) {
    $node = Resolve-NodeDir
    if ($node.Path) {
        $Env:PATH = "$($node.Path)$([System.IO.Path]::PathSeparator)$Env:PATH"
    }
    $resolved = Get-PathNode
    if ($resolved -and $resolved.Version.Major -ge $MinNodeMajor) {
        Write-Good "node    v$($resolved.Version)  ($($node.Why))"
    } elseif ($node.Missing) {
        Write-Warn2 "No usable Node ($MinNodeMajor+) - 'npm run build' in frontend/ will fail."
        Write-Note  "Fix with any of: nvm install $RequiredNodeVersion | https://nodejs.org/en/download | re-run this script without -NoProvision"
    }
}

Write-Host ""
