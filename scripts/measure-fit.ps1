# measure-fit.ps1 -- load-only footprint probe at full context. NO generation (so the
# #27871 depth-262144 abort is not in play; this only allocates the KV + indexer + weights).
# Reports the real carve vs host split so the 260k fit is measured, not estimated.
# Usage: .\measure-fit.ps1 -Shard1 <path> -Draft <path> [-Ctx 262144]
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Shard1, [Parameter(Mandatory)][string]$Draft, [string]$Exe = "llama-server.exe", [string]$RocmBin = "", [int]$Ctx = 262144, [int]$Port = 13398)
$ErrorActionPreference = "Stop"
$exe = $Exe; $draft = $Draft
if ($RocmBin) { $env:PATH = "$RocmBin;" + $env:PATH }
$stamp = Get-Date -Format HHmmss
$log = Join-Path $PSScriptRoot "fit-$stamp.log"

$argStr = "-m `"$Shard1`" -md `"$draft`" --spec-type draft-mtp --spec-draft-n-max 4 " +
          "-c $Ctx -fa on -ctk f16 -ctv f16 --load-mode none -lv 10 --host 127.0.0.1 --port $Port"
$proc = Start-Process -FilePath $exe -ArgumentList $argStr -NoNewWindow -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError "$log.stderr"

# wait for load done or failure
for ($i=0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 3
    $s = Get-Content "$log.stderr" -Raw -ErrorAction SilentlyContinue
    if ($s -match "llama_server: model loaded" -or $s -match "listening on") { break }
    if ($s -match "failed to allocate|out of memory|unable to allocate|std::bad_alloc|terminate called|GGML_ASSERT") { Write-Host "LOAD FAILED (alloc)"; break }
    if ($proc.HasExited) { Write-Host "PROCESS EXITED early (code $($proc.ExitCode))"; break }
}
Start-Sleep -Seconds 2
Write-Host "=== buffer placement (@ ctx $Ctx) ==="
Select-String -Path "$log.stderr" -Pattern "model buffer size|KV buffer size|compute buffer size|indexer|CPU_Mapped|offloaded|failed to allocate|out of memory|abort|GGML_ASSERT" |
    ForEach-Object { $_.Line } | Select-Object -First 30
Write-Host ("host free during load: {0:N1} GB" -f ((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB))
Stop-Process -Id $proc.Id -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Write-Host "stopped."
