# Copies everything the AI Image Generator Suite keeps ONLY on this machine
# (not in git) to a dated backup folder. Copies, never moves or deletes.
#   powershell -ExecutionPolicy Bypass -File tools\backup_local.ps1 [-Dest D:\Backups\AI-Image-Generator-Suite]
# Keys live only in the Windows Credential Manager and are never copied.
param([string]$Dest = "D:\Backups\AI-Image-Generator-Suite")

$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$out = Join-Path $Dest $stamp
if (-not $out -or $out.Length -lt 10) { throw "bad destination" }
New-Item -ItemType Directory -Force $out | Out-Null

$items = @(
    @{ src = Join-Path $root "golden";        dst = "golden" },          # quality-gate reference
    @{ src = Join-Path $root "cache\api";     dst = "cache\api" },       # paid answers
    @{ src = Join-Path $root "tools";         dst = "tools" },
    @{ src = "$env:USERPROFILE\Desktop\Stickers"; dst = "Stickers" }     # sources + Ready finals
)
foreach ($i in $items) {
    if (Test-Path $i.src) {
        robocopy $i.src (Join-Path $out $i.dst) /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $($i.src)" }
        Write-Output ("copied {0} -> {1}" -f $i.src, $i.dst)
    }
}
# git state, so the backup says which code it belongs to
git -C $root log -1 --format="%H %d %s" | Out-File (Join-Path $out "git_head.txt") -Encoding utf8
# safety: no key may be in the backup
$hits = Get-ChildItem $out -Recurse -File -Include *.json,*.txt,*.py,*.md,*.ps1 |
    Select-String -Pattern 'sk-ant-[A-Za-z0-9_-]{20,}','fal_sk_[0-9a-f]+:' -List
if ($hits) { throw "a key-shaped string was found in the backup: $($hits[0].Path)" }
Write-Output "backup complete: $out"
