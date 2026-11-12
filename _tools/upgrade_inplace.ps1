param(
    [switch]$Apply,
    [string]$NewExe = "C:\Users\zyr71\Desktop\MBPK\ManageBac-packer-v0.5.1\dist\ManageBac-packer.exe",
    [string]$Target = "$env:LOCALAPPDATA\ManageBac-packer"
)

# ==================================================================
#  In-place (seamless) upgrade: copy the new exe over the installed one.
#
#  Design notes
#    - Default is DRY-RUN: without -Apply it only reports what it would do.
#    - Backs up the old exe to backup-<timestamp>\ first, so it is reversible.
#    - Touches ONLY the exe. credentials.json and data\ are left untouched,
#      so no re-login is needed.
#    - After copying it verifies size, SHA256, and that credentials/data
#      survived.
#
#  Usage
#    powershell -File upgrade_inplace.ps1            # dry run
#    powershell -File upgrade_inplace.ps1 -Apply     # actually replace
#
#  (Deliberately ASCII-only: Windows PowerShell 5.1 reads .ps1 as ANSI
#   unless the file has a BOM, which corrupts non-ASCII text.)
# ==================================================================

$ErrorActionPreference = "Stop"
$ExeName = "ManageBac-packer.exe"

# Count ONLY user data under data\ -- browser caches change constantly
# (they even shrink when the browser cleans up), so a raw "total entries"
# check produced false alarms. Volatile dirs are excluded.
function Count-UserData([string]$dir) {
    if (-not (Test-Path $dir)) { return -1 }
    $volatile = @('browser-profile', 'webview2', 'schedule-profile', 'recon')
    $n = 0
    Get-ChildItem $dir -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $rel = $_.FullName.Substring($dir.Length).TrimStart('\')
        $top = ($rel -split '\\')[0]
        if ($volatile -notcontains $top) { $n++ }
    }
    return $n
}


function Sha256([string]$path) { (Get-FileHash -Path $path -Algorithm SHA256).Hash }

Write-Host ("=" * 68)
Write-Host "  ManageBac-packer in-place upgrade"
Write-Host ("=" * 68)
Write-Host ("  mode      : " + $(if ($Apply) { "APPLY (will modify files)" } else { "DRY-RUN (no changes)" }))
Write-Host "  new exe   : $NewExe"
Write-Host "  target    : $Target"
Write-Host ""

if (-not (Test-Path $NewExe)) { throw "new exe not found: $NewExe" }
if (-not (Test-Path $Target)) { throw "target dir not found: $Target" }

$targetExe = Join-Path $Target $ExeName
$creds     = Join-Path $Target "credentials.json"
$dataDir   = Join-Path $Target "data"

$newHash = Sha256 $NewExe

# ---- 1. current state ----
Write-Host "--- [1] current state ---"
if (Test-Path $targetExe) {
    $old = Get-Item $targetExe
    Write-Host ("  installed exe : {0,11:N0} bytes  {1}" -f $old.Length, $old.LastWriteTime)
    Write-Host ("  installed sha : {0}" -f (Sha256 $targetExe))
} else {
    Write-Host "  installed exe : [absent - treated as fresh install]"
}
$newItem = Get-Item $NewExe
Write-Host ("  new exe       : {0,11:N0} bytes  {1}" -f $newItem.Length, $newItem.LastWriteTime)
Write-Host ("  new sha       : {0}" -f $newHash)
$SameExe = (Test-Path $targetExe) -and ($newHash -eq (Sha256 $targetExe))
if ($SameExe) {
    Write-Host "  NOTE: installed exe is already identical to the new one."
}
Write-Host ("  credentials   : " + $(if (Test-Path $creds) { "present (kept)" } else { "absent" }))
if (Test-Path $dataDir) {
    $n = (Count-UserData $dataDir)
    Write-Host "  data\         : present (kept), $n user files"
} else {
    Write-Host "  data\         : absent"
}

$credsBefore = if (Test-Path $creds) { Sha256 $creds } else { $null }
$dataBefore  = if (Test-Path $dataDir) { (Count-UserData $dataDir) } else { -1 }

# ---- 2. stop running instance ----
Write-Host ""
Write-Host "--- [2] stop running instance ---"
$running = Get-Process -Name "ManageBac-packer" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host ("  found {0} process(es): {1}" -f @($running).Count, ((@($running) | ForEach-Object { $_.Id }) -join ", "))
    if ($Apply) {
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Write-Host "  stopped"
    } else {
        Write-Host "  [dry-run] would stop them"
    }
} else {
    Write-Host "  none running"
}
foreach ($f in @((Join-Path $Target "app.lock"), (Join-Path $Target "app.pid"))) {
    if (Test-Path $f) {
        if ($Apply) {
            Remove-Item $f -Force -ErrorAction SilentlyContinue
            Write-Host ("  removed single-instance lock: " + (Split-Path $f -Leaf))
        } else {
            Write-Host ("  [dry-run] would remove lock: " + (Split-Path $f -Leaf))
        }
    }
}

#  升级时是我们主动停掉进程，不是崩溃 —— 清掉「未正常结束」标记，
#  否则用户升级完第一次打开会看到「上次异常退出」的假提示。
if ($Apply) {
    foreach ($rel in @("logs\running.marker", "data\running.marker")) {
        $mk = Join-Path $Target $rel
        if (Test-Path $mk) {
            Remove-Item $mk -Force -ErrorAction SilentlyContinue
            Write-Host "  cleared crash marker (upgrade, not a crash)"
        }
    }
}

# ---- 3. back up old exe ----
#  Backups are kept to a small number. Each one is a full ~18 MB executable,
#  and 21 of them had grown to 379 MB inside the install folder. Three is
#  enough to roll back a bad release without turning the folder into an archive.
$KEEP_BACKUPS = 3

function Prune-OldBackups([string]$root, [int]$keep) {
    $all = @(Get-ChildItem (Join-Path $root "backup-*") -Directory -ErrorAction SilentlyContinue |
             Sort-Object Name)
    if ($all.Count -le $keep) { return }
    $drop = $all[0..($all.Count - $keep - 1)]
    foreach ($d in $drop) {
        try {
            Remove-Item $d.FullName -Recurse -Force -ErrorAction Stop
            Write-Host ("  pruned old backup: " + $d.Name)
        } catch {
            Write-Host ("  could not prune " + $d.Name + " (in use?)")
        }
    }
}
$stamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $Target "backup-$stamp"
Write-Host ""
Write-Host "--- [3] back up old exe ---"
if ((Test-Path $targetExe) -and $SameExe) {
    #  Nothing is being replaced (the exe is already identical), so a backup
    #  would be pure clutter -- and repeated upgrades would pile them up.
    Write-Host "  skipped (exe unchanged, no backup needed)"
} elseif (Test-Path $targetExe) {
    Write-Host "  backup dir : $backup"
    if ($Apply) {
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Copy-Item $targetExe (Join-Path $backup $ExeName) -Force
        Write-Host ("  backed up, sha = " + (Sha256 (Join-Path $backup $ExeName)))
    } else {
        Write-Host "  [dry-run] would copy the old exe there"
    }
} else {
    Write-Host "  no old exe, skipped"
}

# ---- 4. copy new exe ----
Write-Host ""
Write-Host "--- [4] install new exe ---"
if ($Apply) {
    if ($SameExe) {
        # Already identical: copying would only risk an IOException while the
        # just-killed process still holds the file handle. Nothing to do.
        Write-Host "  skipped (already identical)"
    } else {
        Copy-Item $NewExe $targetExe -Force
        Write-Host "  copied"
    }
} else {
    Write-Host "  [dry-run] would overwrite $targetExe"
}

# ---- 5. verify ----
Write-Host ""
Write-Host "--- [5] verify ---"
if ($Apply) {
    $ok = $true
    $after = Sha256 $targetExe
    $same  = ($after -eq $newHash)
    $ok = $ok -and $same
    Write-Host ("  installed sha matches new exe : " + $(if ($same) { "YES" } else { "NO" }))
    Write-Host ("  installed size                : {0:N0} bytes" -f (Get-Item $targetExe).Length)

    if ($credsBefore) {
        $credsOk = (Test-Path $creds) -and ((Sha256 $creds) -eq $credsBefore)
        $ok = $ok -and $credsOk
        Write-Host ("  credentials.json untouched    : " + $(if ($credsOk) { "YES (no re-login needed)" } else { "NO" }))
    }
    if ($dataBefore -ge 0) {
        $after2 = (Count-UserData $dataDir)
        $dataOk = $after2 -ge $dataBefore
        $ok = $ok -and $dataOk
        Write-Host ("  data\ user files               : {0} -> {1}  {2}" -f $dataBefore, $after2, $(if ($dataOk) { "(not lost)" } else { "(LOST ITEMS)" }))
    }
    Prune-OldBackups $Target $KEEP_BACKUPS
    Write-Host ""
    Write-Host $(if ($ok) { "  [OK] upgrade done, all checks passed" } else { "  [X] some checks failed, see above" })
    Write-Host "  rollback: copy $backup\$ExeName back to $Target\"
} else {
    Write-Host "  [dry-run finished] nothing was modified. Add -Apply to execute."
}
Write-Host ("=" * 68)

