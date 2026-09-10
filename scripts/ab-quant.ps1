# ab-quant.ps1 -- launch one quant with the MTP drafter, run a FIXED prompt set, report
# decode t/s + draft accept%, stop. Prompts are deterministic (no Get-Random) so IQ4_XS and
# Q4_K_XL see byte-identical inputs -- the only variable is the target quant. Same draft head
# (Q8_0), same flags (n4, p_min 0.75, f16 KV, temp 1.0 thinking-mode).
# Usage: .\ab-quant.ps1 -Shard1 <path> -Draft <path> -Label IQ4_XS
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Shard1,
  [Parameter(Mandatory)][string]$Draft,
  [Parameter(Mandatory)][string]$Label,
  [string]$Exe = "llama-server.exe", [string]$RocmBin = "",
  [int]$Ctx = 65536, [int]$Ub = 512, [int]$NMax = 4, [double]$PMin = 0.75, [int]$Port = 13399
)
$ErrorActionPreference = "Stop"
$exe = $Exe; $draft = $Draft
if ($RocmBin) { $env:PATH = "$RocmBin;" + $env:PATH }
$log = Join-Path $PSScriptRoot "ab-$Label.log"

$argStr = "-a ab -m `"$Shard1`" -md `"$draft`" --spec-type draft-mtp --spec-draft-n-max $NMax --spec-draft-p-min $PMin " +
          "-c $Ctx --parallel 1 -fa on -ctk f16 -ctv f16 --load-mode none -b $Ub -ub $Ub " +
          "--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 --presence-penalty 0.0 --repeat-penalty 1.0 " +
          "--n-predict 10240 --reasoning-budget 6144 --jinja --host 127.0.0.1 --port $Port"
$proc = Start-Process -FilePath $exe -ArgumentList $argStr -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.stderr"
for ($i=0; $i -lt 120; $i++) { Start-Sleep -Seconds 3; try { Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3 | Out-Null; break } catch {} }

# Deterministic filler: a fixed varied technical paragraph, repeated to depth (NOT one word).
$para = "The scheduler allocates a compute buffer before each decode step; the indexer scores every key in the cache, selects the top candidates, and gathers their rows from the sparse table while the router picks a small set of experts for the active token. "
function Filler([int]$reps) { ($para * $reps) }
function Chat($prompt,$maxTok=256){ $b=@{model="ab";messages=@(@{role="user";content=$prompt});max_tokens=$maxTok;stream=$false}|ConvertTo-Json -Depth 6; Invoke-RestMethod "http://127.0.0.1:$Port/v1/chat/completions" -Method Post -ContentType "application/json" -Body $b -TimeoutSec 1800 }
function Acc($t){ $dn=$t.draft_n; $da=$t.draft_n_accepted; if($dn){[math]::Round(100*$da/$dn,1)}else{"n/a"} }

$deep = "Context notes: " + (Filler 260) + " END OF NOTES. Ignore the notes. What is 23 * 19? Answer with just the number."
$prompts = @(
  @{k="shallow-code"; p="Write a Python function merge_intervals(intervals) that merges overlapping [start,end] intervals and returns the merged list sorted by start. Code only."},
  @{k="mid-2k";       p="Context: " + (Filler 60) + " In one sentence, what does the indexer do?"},
  @{k="deep-8k";      p=$deep}
)

Write-Host ("==== $Label ====")
Write-Host "case          | prompt_n | prefill t/s | decode t/s | accept%"
foreach($it in $prompts){
  $r = Chat $it.p 256
  "{0,-13} | {1,8} | {2,11} | {3,10} | {4}" -f $it.k, $r.timings.prompt_n, [math]::Round($r.timings.prompt_per_second,1), [math]::Round($r.timings.predicted_per_second,1), (Acc $r.timings)
}
# warm turn: resend the deep context (cached) + a new short question -> agent-loop case
$warm = $deep + " Also, briefly: is 19 prime? One word."
$r = Chat $warm 128
"{0,-13} | {1,8} | {2,11} | {3,10} | {4}" -f "warm-reuse", $r.timings.prompt_n, [math]::Round($r.timings.prompt_per_second,1), [math]::Round($r.timings.predicted_per_second,1), (Acc $r.timings)

Stop-Process -Id $proc.Id -Force -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4
Write-Host "$Label done, server stopped."
