# probe-mtp.ps1 -- correctness-FIRST validation of the MTP build. Speed is secondary:
# block-14 MTP is the risky path (llama.cpp #27797 slash-flood on HIP multi-turn; apepojken's
# gfx1151 port corrupted to multilingual noise past ~1-2k tokens WHILE showing 2x tok/s -- a
# benchmark that doesn't READ output falsely passes). So every gate reads the text.
#
# Baseline to beat (stock b1326, no MTP, measured 2026-09-08):
#   decode 21.7 @500 / 21.6 @2000 / 20.3 @8000 ; needle-clean ; multi-turn clean.
# MTP is only a win if decode goes UP and coherence holds.
#
# Usage: .\probe-mtp.ps1 [-Port 13399]
[CmdletBinding()]
param([int]$Port = 13399)
$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"

function Chat($messages, $maxTok = 512) {
    $body = @{ model="flashnext-mtp"; messages=$messages; max_tokens=$maxTok; stream=$false } | ConvertTo-Json -Depth 8
    Invoke-RestMethod "$base/v1/chat/completions" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 1800
}
function Accept($t) {
    # field names vary across builds; try the common ones
    $dn = $t.draft_n; if (-not $dn) { $dn = $t.n_draft }
    $da = $t.draft_n_accepted; if (-not $da) { $da = $t.n_draft_accepted }
    if ($dn -and $dn -gt 0) { return [math]::Round(100 * $da / $dn, 1) } else { return $null }
}

# --- GATE 1: coherence + acceptance + speed (single turn) ---
$r = Chat @(@{role="user";content="Write a Python function that returns the nth Fibonacci number iteratively. Code plus one sentence."}) 2048
Write-Host "--- GATE 1: single-turn ---"
Write-Host ("full timings: " + ($r.timings | ConvertTo-Json -Compress))
$acc = Accept $r.timings
Write-Host ("decode t/s : " + $r.timings.predicted_per_second + "  | prefill t/s: " + $r.timings.prompt_per_second + ("  | draft accept: {0}" -f $(if($acc -ne $null){"$acc%"}else{"n/a"})))
Write-Host $r.choices[0].message.content
if ($r.choices[0].message.content -match '/{6,}' -or $r.choices[0].message.content.Length -lt 10) { Write-Host "FAIL: degenerate output" -ForegroundColor Red; exit 1 }
Write-Host "GATE 1 PASS" -ForegroundColor Green

# --- GATE 2: multi-turn (#27797 / apepojken slash-flood guard) ---
$r2 = Chat @(
    @{role="user";content="List three prime numbers between 80 and 100."},
    @{role="assistant";content="83, 89, and 97."},
    @{role="user";content="Multiply the smallest by 4 and show the arithmetic."}
) 1024
Write-Host "--- GATE 2: multi-turn ---"
Write-Host $r2.choices[0].message.content
if ($r2.choices[0].message.content -match '/{6,}') { Write-Host "FAIL: #27797 slash-flood" -ForegroundColor Red; exit 1 }
if ($r2.choices[0].message.content -notmatch '332') { Write-Host "WARN: expected 332 not found" -ForegroundColor Yellow }
Write-Host "GATE 2 PASS" -ForegroundColor Green

# --- GATE 3: decode-vs-depth WITH acceptance (compare to stock 21.7/21.6/20.3) ---
Write-Host "--- GATE 3: depth bands (decode t/s + accept%) ---"
foreach ($tokens in 500, 2000, 8000) {
    $filler = ("granite " * $tokens)
    $r3 = Chat @(@{role="user";content=("S" + (Get-Random) + " ignore filler. " + $filler + " What is 17*13? Number only.")}) 256
    $a = Accept $r3.timings
    "{0,6} tok : decode {1,6} t/s | prefill {2,7} t/s | accept {3,6} | out: {4}" -f $tokens,
        [math]::Round($r3.timings.predicted_per_second,1), [math]::Round($r3.timings.prompt_per_second,1),
        $(if($a -ne $null){"$a%"}else{"n/a"}), (($r3.choices[0].message.content -replace "`n"," ").Substring(0,[Math]::Min(40,$r3.choices[0].message.content.Length)))
}

# --- GATE 4: needle @ ~24k (quality at depth with MTP) ---
$secret = Get-Random -Minimum 100000 -Maximum 999999
$filler2 = ("meadow " * 24000)
$p4 = "Read carefully. " + $filler2.Substring(0,[int]($filler2.Length/2)) + " The access code is $secret. " + $filler2.Substring([int]($filler2.Length/2)) + " What is the access code? Number only."
$r4 = Chat @(@{role="user";content=$p4}) 512
Write-Host "--- GATE 4: needle @ ~24k ---"
Write-Host ("expected $secret -> got: " + $r4.choices[0].message.content)
if ($r4.choices[0].message.content -match "$secret") { Write-Host "GATE 4 PASS" -ForegroundColor Green } else { Write-Host "FAIL: needle missed (MTP corrupting depth?)" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "VERDICT: MTP is a win only if GATE 3 decode > stock (21.7/21.6/20.3) AND all gates coherent."
