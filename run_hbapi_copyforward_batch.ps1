<#
  run_hbapi_copyforward_batch.ps1 -- unattended phased runner. Each phase = one `claude -p` (fresh context).
  Survives usage limits via --session-id/--resume. Halts safely on failure. PURE ASCII (PS 5.1 ANSI rule).
  Launch:  powershell -ExecutionPolicy Bypass -File .\run_hbapi_copyforward_batch.ps1
  Smoke:   powershell -ExecutionPolicy Bypass -File .\run_hbapi_copyforward_batch.ps1 -SmokeTest
#>
param([switch]$SmokeTest)

# EAP MUST be Continue: PS 5.1 turns redirected native stderr (2>&1/*>&1) into error records; git prints
# "Switched to branch" and claude --verbose logs to stderr, so Stop would falsely kill the script.
$ErrorActionPreference = "Continue"

# ---------------- config (EDIT PER PROJECT) ----------------
$Repo          = "E:\tradingsoftware\hummingbot-api"
$PromptFile    = "hbapi_copyforward_claude_code_prompt.md"
$NumImplPhases = 7                        # implementation phases; finalization is phase ($NumImplPhases+1)
$BranchPrefix  = "fix/copyforward-p"      # phase N branch the agent creates = "${BranchPrefix}${N}-*"
$BaseBranch    = "dev"                    # phase branches fork from here
$BaseCreateFrom= "nonkyc"                 # create $BaseBranch from here if missing (falls back to master)
$SyncBaseFrom  = ""                       # ff-only sync $BaseBranch from this before start; "" = skip
$IntegrationBranch = "nonkyc"             # finalization merges $BaseBranch into this; "" = leave on base
$ReportFile    = "REPORT_copyforward.md"  # final report the finalization phase writes
# MODEL TIERS (map each phase to the LOWEST tier that clears its difficulty).
$Model         = "claude-fable-5"         # TIER 1: peak reasoning; reserved for P5 (hook wiring into live deploy flow)
$MidModel      = "claude-opus-4-8"        # TIER 2: elite, ~half Fable's pool burn; DEFAULT for well-
                                          #         specified money/logic phases
$LightModel    = "claude-sonnet-4-6"      # TIER 3: mechanical edits, tests/merge/report
$PhaseEffort   = @{ 1='medium'; 2='high'; 3='high'; 4='medium'; 5='high'; 6='medium'; 7='medium'; 8='low' }
$PhaseModel    = @{ 1=$LightModel; 2=$MidModel; 3=$MidModel; 4=$MidModel; 5=$Model; 6=$LightModel; 7=$MidModel; 8=$LightModel }
$PushToOrigin  = $true
$MaxAttemptsPerPhase  = 5
$ProbeIntervalMinutes = 20
$MaxLimitWaitHours    = 12
$RetryPauseSeconds    = 120
$FableCapFallback = "claude-opus-4-8"     # if a phase's model is CAPPED but this (different
                                          # limit pool) IS available, run that phase on it instead
                                          # of parking. "" disables (park-then-halt). Fires only
                                          # when phase model != fallback.
# -----------------------------------------------------------

Set-Location $Repo
$LogDir    = Join-Path $Repo "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$runStamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$masterLog = Join-Path $LogDir "batch_$runStamp.log"
$FinalPhase = $NumImplPhases + 1

$LimitPatterns = @('usage limit','limit will reset','rate limit','credit balance','insufficient credit',
                   'quota','overloaded','try again later','HTTP 429','off-peak')

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $masterLog -Value $line
}
function Halted($n) { return (Test-Path (Join-Path $Repo ("BATCH_HALT_phase_{0}.md" -f $n))) }
function Test-LimitSignature($p) {
    if (-not (Test-Path $p)) { return $false }
    foreach ($x in $LimitPatterns) { if (Select-String -Path $p -Pattern ([regex]::Escape($x)) -Quiet) { return $true } }
    return $false
}
function Test-NoConversation($p) { if (-not (Test-Path $p)) { return $false }; return (Select-String -Path $p -Pattern 'No conversation found' -Quiet) }
function Invoke-Claude($argList, $logPath) { & claude @argList *>&1 | Tee-Object -FilePath $logPath | Out-Null; return $LASTEXITCODE }
function Test-ClaudeReady($model) {
    $log = Join-Path $LogDir ("probe_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    return ((Invoke-Claude @('-p','Reply with exactly: OK','--model',$model,'--effort','low') $log) -eq 0)
}
function Resolve-UsableModel($model) {
    $deadline = (Get-Date).AddHours($MaxLimitWaitHours)
    while ($true) {
        if (Test-ClaudeReady $model) { return $model }
        if ($FableCapFallback -ne "" -and $FableCapFallback -ne $model -and (Test-ClaudeReady $FableCapFallback)) {
            Log ("    {0} is capped but fallback {1} is available; running this attempt on {1}." -f $model, $FableCapFallback); return $FableCapFallback
        }
        if ((Get-Date) -gt $deadline) { return $null }
        Log ("Claude unavailable (probably usage limit); no fallback usable. Probing again in {0} min (until {1})." -f $ProbeIntervalMinutes, $deadline.ToString("HH:mm"))
        Start-Sleep -Seconds ($ProbeIntervalMinutes * 60)
    }
}
# Returns 'OK' | 'HALT' | 'FAILED'
function Run-Phase($n, $taskText, $effort, $model, $verify) {
    $sid = [guid]::NewGuid().ToString(); $sessionStarted = $false
    $resumeText  = "AUTOMATED BATCH RESUME - Phase $n. Your previous session was interrupted (usage limit or crash). Continue exactly where you left off and complete Phase $n of $PromptFile per its rules and its 'Batch execution mode' section: finish implementation, run comprehensive testing, end with the required merge. If prior partial work is unusable, reset from $BaseBranch and redo. If tests cannot go green after the 5-attempt protocol, write BATCH_HALT_phase_$n.md and stop."
    $retryPrefix = "RETRY NOTE: a previous interrupted attempt may have left branch ${BranchPrefix}${n}-* and/or uncommitted changes. Inspect first; continue it or reset the branch from $BaseBranch, then proceed. "
    for ($a = 1; $a -le $MaxAttemptsPerPhase; $a++) {
        $useModel = Resolve-UsableModel $model
        if ($null -eq $useModel) {
            Log ("HALT: waited {0}h for usage limits on phase {1}; still unavailable (no fallback)." -f $MaxLimitWaitHours, $n)
            Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") "Phase ${n}: claude unavailable for over $MaxLimitWaitHours hours. Re-run the batch to continue; completed phases are already merged to $BaseBranch."
            return 'HALT'
        }
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}_{2}.log" -f $n, $a, $stamp)
        if (-not $sessionStarted) {
            $text = $taskText; if ($a -gt 1) { $text = $retryPrefix + $taskText }
            Log ("=== Phase {0} attempt {1}/{2} FRESH (model={3}, effort={4}, session={5})" -f $n,$a,$MaxAttemptsPerPhase,$useModel,$effort,$sid.Substring(0,8))
            $code = Invoke-Claude @('-p',$text,'--model',$useModel,'--effort',$effort,'--session-id',$sid,'--dangerously-skip-permissions','--verbose') $log
            $sessionStarted = $true
        } else {
            Log ("=== Phase {0} attempt {1}/{2} RESUME (model={3}, effort={4}, session={5})" -f $n,$a,$MaxAttemptsPerPhase,$useModel,$effort,$sid.Substring(0,8))
            $code = Invoke-Claude @('-p',$resumeText,'--resume',$sid,'--model',$useModel,'--effort',$effort,'--dangerously-skip-permissions','--verbose') $log
            if ($code -ne 0 -and (Test-NoConversation $log)) {
                $sid = [guid]::NewGuid().ToString()
                Log ("    resume impossible (no conversation); restarting FRESH (session={0})" -f $sid.Substring(0,8))
                $log = Join-Path $LogDir ("phase_{0:D2}_attempt{1}b_{2}.log" -f $n,$a,$stamp)
                $code = Invoke-Claude @('-p',($retryPrefix+$taskText),'--model',$useModel,'--effort',$effort,'--session-id',$sid,'--dangerously-skip-permissions','--verbose') $log
            }
        }
        Log ("=== Phase {0} attempt {1} returned (CLI exit {2})" -f $n,$a,$code)
        if (Halted $n) { return 'HALT' }
        if (& $verify) { return 'OK' }
        if (Test-LimitSignature $log) { Log ("    attempt {0} hit a usage/rate limit; will wait then RESUME." -f $a) }
        else { Log ("    attempt {0} failed (exit {1}); pausing {2}s before retry." -f $a,$code,$RetryPauseSeconds); Start-Sleep -Seconds $RetryPauseSeconds }
    }
    Log ("HALT: phase {0} exhausted {1} attempts." -f $n,$MaxAttemptsPerPhase)
    Set-Content (Join-Path $Repo "BATCH_HALT_phase_$n.md") "Phase $n did not complete after $MaxAttemptsPerPhase attempts. See batch_logs phase_$('{0:D2}' -f $n)_attempt*.log."
    return 'FAILED'
}

# ---- preflight ----
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Log "ABORT: 'claude' not on PATH."; exit 1 }
if (-not (Test-Path (Join-Path $Repo $PromptFile)))          { Log "ABORT: prompt file missing."; exit 1 }
if ($SmokeTest) {
    Log "SMOKE TEST: verifying the production invocation path (auth, headless, permissions flag, model, effort)..."
    $log = Join-Path $LogDir "smoketest_$runStamp.log"
    $code = Invoke-Claude @('-p','Reply with exactly: OK','--model',$Model,'--effort','low','--dangerously-skip-permissions') $log
    if ($code -eq 0) { Log "SMOKE TEST PASSED." } else { Log ("SMOKE TEST FAILED (exit {0}). Log tail:" -f $code); Get-Content $log -Tail 15 | %{ Log ("    "+$_) }; Log "Likely: skip-permissions never accepted (run once interactive: claude --dangerously-skip-permissions), auth expired (claude login), or usage limit." }
    $sd = (& git status --porcelain --untracked-files=no); if ($sd) { Log "WARNING: modified tracked files; real run ABORTS until committed/stashed:"; $sd | %{ Log "    $_" } }
    exit $code
}
$staleHalts = Get-ChildItem -Path $Repo -Filter "BATCH_HALT_phase_*.md" -ErrorAction SilentlyContinue
if ($staleHalts) { Log "ABORT: stale halt file(s) exist. Read then delete to proceed:"; $staleHalts | %{ Log ("    "+$_.Name) }; exit 1 }
$dirty = (& git status --porcelain --untracked-files=no)
if ($dirty) { Log "ABORT: modified tracked files. Commit or stash first:"; $dirty | %{ Log "    $_" }; exit 1 }
Log ("Batch start. Model={0} Light={1} PushToOrigin={2} repo={3}" -f $Model,$LightModel,$PushToOrigin,$Repo)

# ---- ensure base branch exists; optional ff-only sync ----
& git rev-parse --verify $BaseBranch 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $from = $BaseCreateFrom
    & git rev-parse --verify $from 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $from = "master" }
    Log ("Creating $BaseBranch from $from..."); & git checkout -b $BaseBranch $from 2>&1 | Out-Null
} else { & git checkout $BaseBranch 2>&1 | Out-Null }
if ($SyncBaseFrom -ne "") {
    Log ("Syncing $BaseBranch from $SyncBaseFrom (ff-only)...")
    & git merge --ff-only $SyncBaseFrom 2>&1 | Tee-Object -FilePath (Join-Path $LogDir "sync_$runStamp.log") | Out-Null
    if ($LASTEXITCODE -ne 0) { Log "ABORT: $BaseBranch sync from $SyncBaseFrom is not a fast-forward."; exit 1 }
}

# ---- implementation phases ----
for ($n = 1; $n -le $NumImplPhases; $n++) {
    $devBefore = (& git rev-parse $BaseBranch) | Select-Object -First 1
    $task = @"
AUTOMATED BATCH - single phase. Read $PromptFile in the repo root and execute ONLY Phase $n, following every binding rule and its 'Batch execution mode' section (branch off $BaseBranch, implement, comprehensive testing, the 5-attempt ultrathink protocol, all safety/prohibition rules). The phase MUST end with its ${BranchPrefix}${n}-* branch merged --no-ff into $BaseBranch. Do NOT push. If tests cannot go green after 5 attempts, do NOT merge - write BATCH_HALT_phase_$n.md with a dense summary, then stop.
"@
    $verify = { ((& git rev-parse $BaseBranch) | Select-Object -First 1) -ne $devBefore }.GetNewClosure()
    $r = Run-Phase $n $task $PhaseEffort[$n] $PhaseModel[$n] $verify
    if ($r -ne 'OK') { Log ("Batch stopped at phase {0} ({1})." -f $n,$r); exit 2 }
    Log ("=== Phase {0} merged to {1} ({2})." -f $n,$BaseBranch,((& git rev-parse $BaseBranch) | Select-Object -First 1))
}

# ---- finalization phase ----
if ($IntegrationBranch -ne "") {
    if ($PushToOrigin) { $intClause = "merge $BaseBranch into $IntegrationBranch, then push $BaseBranch and $IntegrationBranch to origin" }
    else               { $intClause = "merge $BaseBranch into $IntegrationBranch (do NOT push)" }
} else {
    if ($PushToOrigin) { $intClause = "push $BaseBranch to origin" } else { $intClause = "leave everything on $BaseBranch (no merge, no push)" }
}
$task12 = @"
AUTOMATED BATCH - finalization (Phase $FinalPhase). Read $PromptFile. Run full comprehensive testing on $BaseBranch. If green, $intClause. Write the final dense per-phase report to $ReportFile in the repo root. If testing fails, write BATCH_HALT_phase_$FinalPhase.md and stop WITHOUT the integration merge.
"@
if ($IntegrationBranch -ne "") { $verifyF = { & git merge-base --is-ancestor $BaseBranch $IntegrationBranch 2>$null; $LASTEXITCODE -eq 0 }.GetNewClosure() }
else                           { $verifyF = { Test-Path (Join-Path $Repo $ReportFile) }.GetNewClosure() }
$rF = Run-Phase $FinalPhase $task12 $PhaseEffort[$FinalPhase] $PhaseModel[$FinalPhase] $verifyF
if ($rF -ne 'OK') { Log ("Batch stopped at finalization ({0})." -f $rF); exit 2 }
Log "=== BATCH COMPLETE. Review batch_logs, REPORT_copyforward.md, and any BATCH_HALT_*.md."
exit 0
