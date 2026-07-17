<#
  run_hbtriage_api_batch.ps1 -- unattended phased IMPLEMENTATION runner with cross-family review.
  Run B of the hb_predeploy triage fix batch: API repo (E:/tradingsoftware/hummingbot-api), base nonkyc.
  PRECONDITION (human gate): Run A (engine repo) is merged AND the human has rebuilt and verified
  runtime docker_service.py == image file (CDX-002). Do not start this run before that verification.
  Each phase = 3 fresh headless calls: AUTHOR implements (write) -> REVIEWER interrogates the diff
  (read-only, opposing model family) -> AUTHOR adjudicates every finding (reject with rationale, or agree
  and fix), re-tests, and merges. The author always has final say; every rejection is recorded.
  V2 hardening (proved by the red/blue V3 runner): prompts as raw UTF-8 bytes through STDIN, never argv; a
  raw System.Diagnostics.Process adapter returns trustworthy exit codes; watchdogs on every model call;
  fatal config/auth/model errors fail fast while limits/transients retry; .NET SHA-256 (never Get-FileHash);
  exact sentinels + provenance sidecars; usage-limit fallback constrained to the author's own family so the
  cross-family opposition can never silently collapse. PURE ASCII.
  Launch:  powershell -ExecutionPolicy Bypass -File .\run_hbtriage_api_batch.ps1
  Smoke:   powershell -ExecutionPolicy Bypass -File .\run_hbtriage_api_batch.ps1 -SmokeTest
  Full:    add -SmokeFull to smoke the REAL configured models, premium ones included (proves Fable is
           reachable -- phases 7 and 8 request Fable).
  Fresh:   add -Fresh to redo phases even if their branch/merge already exists.
#>
param([switch]$SmokeTest,[switch]$SmokeFull,[switch]$Fresh)

# EAP MUST be Continue: PS 5.1 turns redirected native stderr into error records; git prints "Switched to
# branch" and the CLIs log to stderr, so Stop would falsely kill the script.
$ErrorActionPreference = "Continue"

# Windows PowerShell 5.1 can inherit PS7's Core-only Utility module ahead of its own module roots and then
# lose legacy cmdlets (Get-FileHash vanishes). Strip only PS7's installation module root from THIS process;
# machine/user config is untouched. Hashing below uses .NET directly and never depends on Get-FileHash.
$moduleParts=@($env:PSModulePath -split [regex]::Escape([string][IO.Path]::PathSeparator) | Where-Object { $_ })
$RemovedCoreModulePaths=@($moduleParts | Where-Object { $_ -match '(?i)[\\/]PowerShell[\\/]7[\\/]Modules[\\/]?$' })
$env:PSModulePath=(@($moduleParts | Where-Object { $_ -notmatch '(?i)[\\/]PowerShell[\\/]7[\\/]Modules[\\/]?$' }) -join [IO.Path]::PathSeparator)

# Prompts are written as raw UTF-8 bytes to the child process STDIN, never placed in argv.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

# ---------------- config (hbtriage_api / Run B) ----------------
$Repo          = "E:/tradingsoftware/hummingbot-api"
$Name          = "hbtriage_api"
$PromptFile    = "hbtriage_api_batch_prompt.md"
$NumImplPhases = 9                        # finalization is phase 10
$BranchPrefix  = "fix/hbtriage-p"         # verified: matches NO existing ref in this repo (the stale
                                          # fix/copyforward-p* branches do NOT match this prefix)
$BaseBranch    = "nonkyc"                 # pinned review base; NEVER create or fast-forward it
$BaseCreateFrom= ""                       # EMPTY on purpose: if nonkyc is missing the runner ABORTS
$SyncBaseFrom  = ""                       # never sync nonkyc
$IntegrationBranch = ""                   # land on nonkyc only; promotion is the human's, by hand
$ReportFile    = "REPORT_hbtriage_api.md"
$RecordsDir    = "batch_reviews"
$PushToOrigin  = $false

# ---- MODEL PALETTE + FAMILY MAP (verified against claude 2.1.210 / codex-cli 0.144.4) ----
$FABLE   = "claude-fable-5"
$OPUS    = "claude-opus-4-8"
$SONNET  = "claude-sonnet-4-6"
$GPT56   = "gpt-5.6-sol"
$ModelFamily = @{
  "claude-fable-5"="claude"; "claude-opus-4-8"="claude"; "claude-sonnet-4-6"="claude";
  "claude-haiku-4-5-20251001"="claude"; "gpt-5.6-sol"="codex"
}

# ---- AUTHOR / REVIEWER: the opposition invariant. These MUST differ. Preflight asserts it. ----
$AuthorFamily   = "claude"
$ReviewerFamily = "codex"

# Per-phase plans (1=CDX-001+P1 exclusive target, 2=CDX-007 path, 3=CDX-008 id, 4=CDX-M02 ledger envelope,
# 5=small guards, 6=coupling+drift, 7=CDX-005 retirement FSM [FABLE], 8=CDX-006 accounting [FABLE],
# 9=CDX-013 migrations, 10=finalization). A Fable cap falls back to Opus (same family; opposition intact).
$PhaseEffort       = @{ 1='xhigh'; 2='high'; 3='medium'; 4='high'; 5='medium'; 6='medium'; 7='xhigh'; 8='high'; 9='high'; 10='medium' }
$PhaseModel        = @{ 1=$OPUS; 2=$OPUS; 3=$OPUS; 4=$OPUS; 5=$OPUS; 6=$OPUS; 7=$FABLE; 8=$FABLE; 9=$OPUS; 10=$SONNET }
$PhaseReviewEffort = @{ 1='high'; 2='high'; 3='high'; 4='high'; 5='medium'; 6='medium'; 7='high'; 8='high'; 9='high' }
$PhaseReviewModel  = @{ 1=$GPT56; 2=$GPT56; 3=$GPT56; 4=$GPT56; 5=$GPT56; 6=$GPT56; 7=$GPT56; 8=$GPT56; 9=$GPT56 }

# Usage-limit fallback, PER FAMILY, same-family only (preflight asserts). codex has no fallback model.
$FamilyFallback = @{ "claude"=$OPUS; "codex"="" }

# SMOKE SUBSTITUTIONS (-SmokeTest only; -SmokeFull bypasses). Smoke proves transport/auth/adapter, not
# model quality; Fable is stood down to Opus (same family; also exactly the model a Fable cap falls back to).
$SmokeModelSubstitute = @{ "claude-fable-5" = $OPUS }

$MaxAttemptsPerPhase  = 5
$ProbeIntervalMinutes = 20
$MaxLimitWaitHours    = 12
$MaxStepElapsedHours  = 16
$RetryPauseSeconds    = 120
$ProbeTimeoutSeconds  = 180
$SmokeTimeoutSeconds  = 900
$InvocationTimeoutSeconds = 14400
$MaxInlineDiffBytes   = 400000
$CodexNetworkAccess   = $false
$CodexSupportsResume  = $true             # verified: 0.144.4 supports `codex exec resume <SESSION_ID> -`
$HaltOnRejectedCritical = $false
# -----------------------------------------------------------

Set-Location $Repo
$RecordsPath = Join-Path $Repo $RecordsDir
$LogDir      = Join-Path $Repo "batch_logs"
New-Item -ItemType Directory -Force -Path $RecordsPath | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$runStamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$masterLog  = Join-Path $LogDir ("batch_{0}.log" -f $runStamp)
$FinalPhase = $NumImplPhases + 1

# Failure signatures. Fatal errors require configuration/user action and must never sleep for hours.
$ClaudeLimitPatterns = @('usage limit','limit will reset','rate limit','quota','HTTP 429','off-peak')
$CodexLimitPatterns  = @('rate limit','rate_limit','usage limit','quota','429','Too Many Requests','insufficient_quota')
$TransientPatterns   = @('overloaded','over capacity','try again later','service unavailable','HTTP 502','HTTP 503','HTTP 504','connection reset','ECONNRESET','network error')
$FatalPatterns       = @('unexpected argument','unrecognized option','unknown option','invalid value','invalid model','model not found','unknown model','not available for your account','authentication failed','not authenticated','not logged in','login required','unauthorized','forbidden','invalid api key','insufficient credit','credit balance','cannot find path','path not found','permission denied','access denied','ENOENT')

function Log($msg){ $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"),$msg; Write-Host $line; Add-Content -Path $masterLog -Value $line }
if($RemovedCoreModulePaths.Count -gt 0){ Log ("Windows PowerShell compatibility: removed PS7 module root(s) from this process PSModulePath: {0}" -f ($RemovedCoreModulePaths -join '; ')) }
function Halted($n){ return (Test-Path (Join-Path $Repo ("BATCH_HALT_phase_{0}.md" -f $n))) }
function Write-Halt($n,$message){ $p=Join-Path $Repo ("BATCH_HALT_phase_{0}.md" -f $n); [IO.File]::WriteAllText($p,[string]$message,$Utf8NoBom) }
function Test-Pattern($path,$patterns){
    if(-not (Test-Path $path)){ return $false }
    foreach($x in $patterns){ if(Select-String -Path $path -Pattern ([regex]::Escape($x)) -Quiet){ return $true } }
    return $false
}
function Test-NoConversation($path){
    return (Test-Pattern $path @('No conversation found','session not found','no such session','No previous session','conversation not found'))
}
function Get-Sha256Hex($file){
    $stream=$null; $sha=$null
    try {
        $stream=[IO.File]::Open($file,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        $sha=[Security.Cryptography.SHA256]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant())
    } finally { if($sha){ $sha.Dispose() }; if($stream){ $stream.Dispose() } }
}
function Get-MetaPath($file){ return ("{0}.meta.json" -f $file) }
function Test-SentinelComplete($file,$sentinel){
    if(-not (Test-Path $file)){ return $false }
    if((Get-Item $file).Length -lt 200){ return $false }
    try { $text=[IO.File]::ReadAllText($file) } catch { return $false }
    # The sentinel must be the entire final line. Permit only the line terminator produced by capture.
    $pattern='(?:^|\r?\n)' + [regex]::Escape($sentinel) + '(?:\r?\n)?\z'
    return [regex]::IsMatch($text,$pattern)
}
function Read-StepMetadata($file){
    $mp=Get-MetaPath $file
    if(-not (Test-Path $mp)){ return $null }
    try { return (Get-Content $mp -Raw | ConvertFrom-Json) } catch { return $null }
}
function Write-StepMetadata($step,$result,$usedModel,$attempt,$startedUtc,$completedUtc){
    $hash=Get-Sha256Hex $step.Out
    $meta=[ordered]@{
        schema_version=2; phase=[int]$step.N; step=[string]$step.Name; engine=[string]$step.Engine
        requested_model=[string]$step.Model; actual_requested_model=[string]$usedModel
        requested_effort=[string]$step.Effort; write_mode=[bool]$step.Write
        actual_models=@($result.ActualModels); actual_model_source=[string]$result.ActualModelSource
        cli_version=[string]$result.CliVersion; session_id=[string]$result.SessionId
        attempt=[int]$attempt; started_utc=$startedUtc; completed_utc=$completedUtc
        report_sha256=$hash; report_file=[IO.Path]::GetFileName($step.Out)
    }
    [IO.File]::WriteAllText((Get-MetaPath $step.Out),($meta | ConvertTo-Json -Depth 6),$Utf8NoBom)
}
function Test-StepArtifact($file,$sentinel){
    if(-not (Test-SentinelComplete $file $sentinel)){ return $false }
    $meta=Read-StepMetadata $file
    if($null -eq $meta){ return $false }
    if(-not $meta.actual_models -or -not $meta.cli_version -or -not $meta.report_sha256){ return $false }
    try { $hash=Get-Sha256Hex $file } catch { return $false }
    return ($hash -eq ([string]$meta.report_sha256).ToLowerInvariant())
}

# Windows-native argv quoting for ProcessStartInfo.Arguments. Prompts never pass through this; they use STDIN.
function Quote-NativeArg([string]$value){
    if([string]::IsNullOrEmpty($value)){ return '""' }
    if($value -notmatch '[\s"]'){ return $value }
    $b=New-Object Text.StringBuilder
    [void]$b.Append([char]34)
    $slashes=0
    foreach($ch in $value.ToCharArray()){
        if($ch -eq [char]92){ $slashes++; continue }
        if($ch -eq [char]34){
            for($i=0;$i -lt (2*$slashes+1);$i++){ [void]$b.Append([char]92) }
            [void]$b.Append([char]34); $slashes=0; continue
        }
        for($i=0;$i -lt $slashes;$i++){ [void]$b.Append([char]92) }
        $slashes=0; [void]$b.Append($ch)
    }
    for($i=0;$i -lt (2*$slashes);$i++){ [void]$b.Append([char]92) }
    [void]$b.Append([char]34)
    return $b.ToString()
}
function Invoke-NativeWithInput($exe,$arguments,$stdinText,$workingDir,$stdoutPath,$stderrPath,$timeoutSeconds){
    foreach($p in @($stdoutPath,$stderrPath)){ if(Test-Path $p){ Remove-Item $p -Force } }
    $argLine=(($arguments | ForEach-Object { Quote-NativeArg ([string]$_) }) -join ' ')
    $proc=$null; $stdinStream=$null; $outTask=$null; $errTask=$null; $timedOut=$false; $code=125; $startError=$null
    $originalInputEncoding=$null; $consoleInputChanged=$false
    $stdoutText=''; $stderrText=''; $started=$false
    try {
        $psi=New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName=[string]$exe; $psi.Arguments=$argLine; $psi.WorkingDirectory=$workingDir
        $psi.UseShellExecute=$false; $psi.CreateNoWindow=$true
        $psi.RedirectStandardInput=$true; $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true
        $psi.StandardOutputEncoding=$Utf8NoBom; $psi.StandardErrorEncoding=$Utf8NoBom
        $proc=New-Object System.Diagnostics.Process; $proc.StartInfo=$psi
        # .NET Framework builds redirected StandardInput from Console.InputEncoding and would otherwise
        # prepend that encoding's preamble. Pin it to BOM-less UTF-8 only while the stream is constructed.
        try {
            $originalInputEncoding=[Console]::InputEncoding
            [Console]::InputEncoding=$Utf8NoBom; $consoleInputChanged=$true
            if(-not $proc.Start()){ throw 'System.Diagnostics.Process.Start returned false.' }
            $started=$true
            $stdinStream=$proc.StandardInput.BaseStream
        } finally {
            if($consoleInputChanged){ try { [Console]::InputEncoding=$originalInputEncoding } catch {}; $consoleInputChanged=$false }
        }
        # Drain both pipes concurrently so a verbose CLI cannot deadlock on a full stdout/stderr buffer.
        $outTask=$proc.StandardOutput.ReadToEndAsync(); $errTask=$proc.StandardError.ReadToEndAsync()
        try {
            $stdinBytes=$Utf8NoBom.GetBytes([string]$stdinText)
            if($stdinBytes.Length -gt 0){ $stdinStream.Write($stdinBytes,0,$stdinBytes.Length) }
            $stdinStream.Flush()
        } finally { if($stdinStream){ try { $stdinStream.Close() } catch {} } }
        $finished=$proc.WaitForExit([int]($timeoutSeconds*1000))
        if(-not $finished){
            $timedOut=$true
            & taskkill.exe /PID $proc.Id /T /F 2>$null | Out-Null
            if(-not $proc.WaitForExit(10000)){ try { $proc.Kill() } catch {}; [void]$proc.WaitForExit(5000) }
            $code=124
        } else {
            $proc.WaitForExit()
            # Raw Process retains the true native exit code on affected Windows PowerShell 5.1 hosts.
            $code=[int]$proc.ExitCode
        }
    } catch {
        $startError=$_.Exception.Message
        if($started -and $proc){
            try {
                if(-not $proc.HasExited){ $proc.Kill(); [void]$proc.WaitForExit(5000) }
                if($proc.HasExited){ $code=[int]$proc.ExitCode }
            } catch { $code=125 }
        }
    } finally {
        if($outTask){ try { if(-not $outTask.IsCompleted){ [void]$outTask.Wait(5000) }; if($outTask.Status -eq [Threading.Tasks.TaskStatus]::RanToCompletion){ $stdoutText=[string]$outTask.Result } } catch {} }
        if($errTask){ try { if(-not $errTask.IsCompleted){ [void]$errTask.Wait(5000) }; if($errTask.Status -eq [Threading.Tasks.TaskStatus]::RanToCompletion){ $stderrText=[string]$errTask.Result } } catch {} }
        if($startError){ $stderrText += (("`r`nPROCESS ERROR: {0}`r`n" -f $startError)) }
        [IO.File]::WriteAllText($stdoutPath,$stdoutText,$Utf8NoBom)
        [IO.File]::WriteAllText($stderrPath,$stderrText,$Utf8NoBom)
        if($proc){ $proc.Dispose() }
    }
    return [pscustomobject]@{ ExitCode=$code; TimedOut=$timedOut; StartError=$startError; StdoutPath=$stdoutPath; StderrPath=$stderrPath }
}
function Merge-ProcessLogs($stdoutPath,$stderrPath,$rawLog){
    $b=New-Object Text.StringBuilder
    if(Test-Path $stderrPath){ [void]$b.AppendLine('===== STDERR ====='); [void]$b.AppendLine([IO.File]::ReadAllText($stderrPath,[Text.Encoding]::UTF8)) }
    if(Test-Path $stdoutPath){ [void]$b.AppendLine('===== STDOUT ====='); [void]$b.AppendLine([IO.File]::ReadAllText($stdoutPath,[Text.Encoding]::UTF8)) }
    [IO.File]::WriteAllText($rawLog,$b.ToString(),$Utf8NoBom)
}

# ---- engine adapters: verified layout for Codex CLI 0.144.4 and Claude Code 2.1.210 ----
# $write selects the posture: author steps get write access, review steps are hard read-only.
function Invoke-Codex($mode,$text,$model,$effort,$outFile,$rawLog,$sid,$timeoutSeconds,$write){
    # Global Codex flags MUST precede `exec` on 0.144.4. Exec/resume flags follow their subcommand.
    $sandbox=if($write){ 'workspace-write' } else { 'read-only' }
    $a=@('--ask-for-approval','never','--sandbox',$sandbox,'--cd',$Repo,'-m',$model,
         '-c',("model_reasoning_effort=`"{0}`"" -f $effort))
    if($write -and $CodexNetworkAccess){ $a += @('-c','sandbox_workspace_write.network_access=true') }
    $a += 'exec'
    if(($mode -eq 'resume') -and $CodexSupportsResume -and $sid){
        $a += 'resume'; $a += @('--json','--output-last-message',$outFile,$sid,'-')
    } else {
        $a += @('--json','--output-last-message',$outFile,'-')
    }
    $stdout="$rawLog.stdout.jsonl"; $stderr="$rawLog.stderr"
    $native=Invoke-NativeWithInput 'codex' $a $text $Repo $stdout $stderr $timeoutSeconds
    Merge-ProcessLogs $stdout $stderr $rawLog
    $threadId=$sid; $models=@(); $source='codex-jsonl'
    if(Test-Path $stdout){
        foreach($line in (Get-Content $stdout -Encoding UTF8)){
            if([string]::IsNullOrWhiteSpace($line)){ continue }
            try { $obj=$line | ConvertFrom-Json } catch { continue }
            if($obj.type -eq 'thread.started' -and $obj.thread_id){ $threadId=[string]$obj.thread_id }
            if(($obj.PSObject.Properties.Name -contains 'model') -and $obj.model){ $models += [string]$obj.model }
            if($obj.item -and ($obj.item.PSObject.Properties.Name -contains 'model') -and $obj.item.model){ $models += [string]$obj.item.model }
        }
    }
    $models=@($models | Select-Object -Unique)
    if($models.Count -eq 0){ $models=@($model); $source='requested-model-hard-pin; JSONL emitted no model field' }
    return [pscustomobject]@{ ExitCode=$native.ExitCode; TimedOut=$native.TimedOut; StartError=$native.StartError; RawLog=$rawLog; ActualModels=$models; ActualModelSource=$source; SessionId=$threadId; CliVersion=$CodexCliVersion }
}
function Invoke-ClaudeEngine($mode,$text,$model,$effort,$outFile,$rawLog,$sid,$timeoutSeconds,$write){
    # No prompt argument: -p reads the UTF-8 STDIN. JSON .result is the captured final message.
    # write => --dangerously-skip-permissions (must have been accepted once interactively).
    # read  => --permission-mode plan (tool-level no-write guard). NEVER give a reviewer write access.
    $perm=if($write){ @('--dangerously-skip-permissions') } else { @('--permission-mode','plan') }
    if($mode -eq 'resume'){ $a=@('-p','--resume',$sid,'--model',$model,'--effort',$effort) }
    else                  { $a=@('-p','--session-id',$sid,'--model',$model,'--effort',$effort) }
    $a += $perm; $a += @('--output-format','json')
    $stdout="$rawLog.stdout.json"; $stderr="$rawLog.stderr"
    $native=Invoke-NativeWithInput 'claude' $a $text $Repo $stdout $stderr $timeoutSeconds
    Merge-ProcessLogs $stdout $stderr $rawLog
    $obj=$null; $models=@(); $sessionId=$sid; $source='claude-json'
    if((Test-Path $stdout) -and ((Get-Item $stdout).Length -gt 0)){
        try { $obj=([IO.File]::ReadAllText($stdout,[Text.Encoding]::UTF8) | ConvertFrom-Json) } catch { $obj=$null }
    }
    if($obj){
        if(($obj.PSObject.Properties.Name -contains 'result') -and $obj.result){ [IO.File]::WriteAllText($outFile,[string]$obj.result,$Utf8NoBom) }
        if(($obj.PSObject.Properties.Name -contains 'model') -and $obj.model){ $models += [string]$obj.model }
        if(($obj.PSObject.Properties.Name -contains 'modelUsage') -and $obj.modelUsage){ $models += @($obj.modelUsage.PSObject.Properties.Name) }
        if(($obj.PSObject.Properties.Name -contains 'session_id') -and $obj.session_id){ $sessionId=[string]$obj.session_id }
    }
    $models=@($models | Select-Object -Unique)
    if($models.Count -eq 0){ $source='missing; Claude JSON had no model/modelUsage fields' }
    return [pscustomobject]@{ ExitCode=$native.ExitCode; TimedOut=$native.TimedOut; StartError=$native.StartError; RawLog=$rawLog; ActualModels=$models; ActualModelSource=$source; SessionId=$sessionId; CliVersion=$ClaudeCliVersion }
}
function Invoke-Engine($engine,$mode,$text,$model,$effort,$outFile,$rawLog,$sid,$timeoutSeconds,$write){
    if($engine -eq 'codex'){ return (Invoke-Codex $mode $text $model $effort $outFile $rawLog $sid $timeoutSeconds $write) }
    return (Invoke-ClaudeEngine $mode $text $model $effort $outFile $rawLog $sid $timeoutSeconds $write)
}
function Test-RequestedModelObserved($requested,$actualModels){
    foreach($actual in @($actualModels)){
        $value=[string]$actual
        if($value.Equals([string]$requested,[StringComparison]::OrdinalIgnoreCase)){ return $true }
        # Providers may append a dated/versioned suffix to the exact requested model id.
        if($value.StartsWith(([string]$requested + '-'),[StringComparison]::OrdinalIgnoreCase)){ return $true }
    }
    return $false
}
function Get-FailureClass($result,$engine){
    if($result.TimedOut){ return 'TIMEOUT' }
    if($result.StartError){ return 'FATAL' }
    if($null -eq $result.ExitCode){ return 'FATAL' }
    if([int]$result.ExitCode -eq 0){ return 'OK' }
    if(Test-Pattern $result.RawLog $FatalPatterns){ return 'FATAL' }
    $limits=if($engine -eq 'codex'){ $CodexLimitPatterns } else { $ClaudeLimitPatterns }
    if(Test-Pattern $result.RawLog $limits){ return 'LIMIT' }
    if(Test-Pattern $result.RawLog $TransientPatterns){ return 'TRANSIENT' }
    return 'FATAL'
}
function Get-FailureTail($path){
    if(-not (Test-Path $path)){ return 'no log output' }
    return ((Get-Content $path -Tail 10) -join ' | ')
}
function Test-EngineReady($engine,$model){
    $stamp=Get-Date -Format 'yyyyMMddHHmmssfff'
    $log=Join-Path $LogDir ("probe_{0}_{1}.log" -f $engine,$stamp)
    $out=Join-Path $LogDir ("probe_{0}_{1}.out" -f $engine,$stamp)
    $sid=if($engine -eq 'claude'){ [guid]::NewGuid().ToString() } else { $null }
    # Probes are always read-only: availability must never be tested with a write-capable session.
    $result=Invoke-Engine $engine 'fresh' 'Return exactly READY and nothing else.' $model 'low' $out $log $sid $ProbeTimeoutSeconds $false
    $outputOk=$false
    if(Test-Path $out){ try { $outputOk=([IO.File]::ReadAllText($out).Trim() -ceq 'READY') } catch { $outputOk=$false } }
    $modelOk=Test-RequestedModelObserved $model $result.ActualModels
    $ready=($null -ne $result.ExitCode -and [int]$result.ExitCode -eq 0 -and -not $result.TimedOut -and -not $result.StartError -and $outputOk -and $modelOk)
    $class=Get-FailureClass $result $engine
    if(($null -ne $result.ExitCode) -and ([int]$result.ExitCode -eq 0) -and -not $ready){
        $class='FATAL'
        Add-Content -Path $log -Value ("FATAL adapter validation: ready_output={0}; requested_model_observed={1}; actual_models={2}" -f $outputOk,$modelOk,(@($result.ActualModels)-join ','))
    }
    return [pscustomobject]@{ Ready=$ready; Class=$class; Result=$result }
}
function Resolve-UsableModel($engine,$model,$stepDeadline){
    # Only confirmed limit/transient failures wait. Parser, auth, path and model errors fail immediately.
    # A cap falls back ONLY within the same family, so author/reviewer opposition is always preserved.
    $deadline=(Get-Date).AddHours($MaxLimitWaitHours)
    if($stepDeadline -lt $deadline){ $deadline=$stepDeadline }
    $fallback=[string]$FamilyFallback[$engine]
    while($true){
        $probe=Test-EngineReady $engine $model
        if($probe.Ready){ return [pscustomobject]@{ Status='READY'; Model=$model; Detail=$null } }
        if($probe.Class -eq 'FATAL'){ return [pscustomobject]@{ Status='FATAL'; Model=$null; Detail=(Get-FailureTail $probe.Result.RawLog) } }
        if($fallback -and ($fallback -ne $model) -and ([string]$ModelFamily[$fallback] -eq $engine)){
            $fp=Test-EngineReady $engine $fallback
            if($fp.Ready){
                Log ("    {0} is capped/unavailable; running this attempt on same-family fallback {1} (opposition preserved)." -f $model,$fallback)
                return [pscustomobject]@{ Status='READY'; Model=$fallback; Detail='same-family-fallback' }
            }
        }
        if((Get-Date) -ge $deadline){ return [pscustomobject]@{ Status='EXPIRED'; Model=$null; Detail=(Get-FailureTail $probe.Result.RawLog) } }
        $remain=[int][Math]::Max(1,($deadline-(Get-Date)).TotalSeconds)
        $sleep=[int][Math]::Min(($ProbeIntervalMinutes*60),$remain)
        Log ("{0}/{1} unavailable ({2}); no usable same-family fallback. Retry in {3}s, deadline {4}." -f $engine,$model,$probe.Class,$sleep,$deadline.ToString('HH:mm'))
        Start-Sleep -Seconds $sleep
    }
}

# ---- git helpers ----
function Get-PhaseBranch($n){
    $pat=("refs/heads/{0}{1}-*" -f $BranchPrefix,$n)
    return ((& git for-each-ref --format='%(refname:short)' $pat) | Select-Object -First 1)
}
function Test-PhaseImplemented($n){
    $b=Get-PhaseBranch $n
    if(-not $b){ return $false }
    $c=((& git rev-list --count ("{0}..{1}" -f $BaseBranch,$b)) | Select-Object -First 1)
    return ([int]$c -gt 0)
}
function Test-PhaseComplete($n){
    # A phase counts as done ONLY when its adjudication record exists AND its branch is an ancestor of the
    # base branch. Do NOT gate on `merge-base --is-ancestor` alone: a branch that was created but never
    # committed to is ALSO trivially an ancestor, so a phase that crashed during its implement step would
    # look "already merged" and be silently skipped on the next run. The record is the real proof.
    if(-not (Test-Path (Get-AdjFile $n))){ return $false }
    $b=Get-PhaseBranch $n
    if(-not $b){ return $false }
    & git merge-base --is-ancestor $b $BaseBranch 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}
function Get-RepoState(){
    return [pscustomobject]@{
        Head   = ((& git rev-parse HEAD) | Select-Object -First 1)
        Branch = ((& git rev-parse --abbrev-ref HEAD) | Select-Object -First 1)
        Dirty  = (((& git status --porcelain --untracked-files=no) | Out-String).Trim())
    }
}
function Get-PhaseDiffText($n){
    $b=Get-PhaseBranch $n
    if(-not $b){ return "(no branch found for phase $n)" }
    $range=("{0}...{1}" -f $BaseBranch,$b)
    $stat=(((& git diff --stat $range) | Out-String).Trim())
    $full=((& git diff $range) | Out-String)
    if($full.Length -gt $MaxInlineDiffBytes){
        return ("DIFF TOO LARGE TO INLINE ({0} bytes). Summary follows; run `git diff {1}` yourself (read-only) to read it.`r`n`r`n{2}" -f $full.Length,$range,$stat)
    }
    return ("DIFF STAT`r`n{0}`r`n`r`nFULL DIFF ({1})`r`n{2}" -f $stat,$range,$full)
}

# ---- prompt assembly: everything a step needs is INLINED ----
$PromptBody = Get-Content (Join-Path $Repo $PromptFile) -Raw
function Get-ReviewSentinel($n){ return ("<!-- BATCH_PHASE_{0}_REVIEW_COMPLETE name={1} -->" -f $n,$Name) }
function Get-AdjSentinel($n){ return ("<!-- BATCH_PHASE_{0}_ADJUDICATION_COMPLETE name={1} -->" -f $n,$Name) }
function Get-ReviewFile($n){ return (Join-Path $RecordsPath ("REVIEW_phase_{0:D2}_{1}.md" -f $n,$Name)) }
function Get-AdjFile($n){ return (Join-Path $RecordsPath ("ADJUDICATION_phase_{0:D2}_{1}.md" -f $n,$Name)) }
function Get-ImplFile($n){ return (Join-Path $RecordsPath ("IMPL_phase_{0:D2}_{1}.md" -f $n,$Name)) }

function Build-ImplPrompt($n){
    return @"
AUTOMATED BATCH - single step. You are the AUTHOR for Phase $n, step A (IMPLEMENT).
Read $PromptFile in the repo root and execute ONLY Phase $n, following every binding rule and its 'Batch
execution mode' section: branch off $BaseBranch as ${BranchPrefix}${n}-<phase-slug>, implement, create/update
tests, run comprehensive testing, and use the 5-attempt ultrathink protocol on failure. Observe every safety
invariant and do-not-touch rule.
COMMIT your work on the phase branch. Do NOT merge and do NOT push -- an opposing-family reviewer will
interrogate your diff next, and you will get the chance to answer it before anything merges.
If tests cannot go green after 5 attempts, do NOT commit broken work: make your FINAL MESSAGE begin with
'BATCH_HALT phase=$n reason=' and stop. Do not write a halt file yourself; the runner owns halt files.
Your final message should be a dense summary of what you implemented and the test result line.
"@
}
function Build-ReviewPrompt($n){
    $sb=New-Object System.Text.StringBuilder
    [void]$sb.AppendLine(("AUTOMATED BATCH - single step. You are the REVIEWER for Phase {0}, step B. You are STRICTLY READ-ONLY." -f $n))
    [void]$sb.AppendLine("An opposing model family just wrote the diff below. Interrogate it. You may READ anything in the repo and run read-only inspections, but you may NOT create, edit, move or delete ANY file, and you may NOT run any state-mutating command (no writes, installs, git commits/checkouts, formatters). A git tripwire aborts the whole run if the tree moves during your review.")
    [void]$sb.AppendLine("Use the 'Review schema' in the BATCH PROMPT below, verbatim. Default to skepticism, but do NOT invent findings: an empty report is a valid, respected outcome -- say 'no findings above the bar' if that is the truth. Report Medium+ only (a Low only if it is a genuine latent bug). Never report pure style. Every claim must cite file:line.")
    [void]$sb.AppendLine("Also judge SPEC CONFORMANCE: does this diff actually implement Phase $n as specified?")
    [void]$sb.AppendLine("TEST INTEGRITY AUDIT -- MANDATORY, and your single highest-value job. This phase arrived with passing tests; passing tests are NOT evidence the code works, only that the tests ran. A test that cannot fail is worse than no test, because it manufactures false confidence. Run the 'TEST INTEGRITY AUDIT' section of the BATCH PROMPT below in full against EVERY added or changed test.")
    [void]$sb.AppendLine("For each one, answer the falsification question: 'if I broke the exact behavior this test claims to verify, would THIS test fail?' If no, or you cannot tell, file a test-theater finding and name the EXACT single-line mutation to the implementation (file:line) that the test should catch. You are read-only and cannot run anything, so reason statically and hand over a precise, executable experiment: the author is REQUIRED to apply your mutation, run the test, and report the result. 'Break it somehow' is useless.")
    [void]$sb.AppendLine("Hunt the named smoke-and-mirrors patterns explicitly: asserts nothing / tests the mock rather than the code / over-mocked so no real path runs / cannot-fail assertions / vacuous empty input / golden-output change-detectors whose expected values came from RUNNING the code instead of the SPEC / assertions weaker than the spec / skipped or commented-out asserts / wrong subject / happy-path only. Severity follows what the un-covered code could do: a vacuous test over money, data-loss or auth logic is High or Critical, never Low.")
    [void]$sb.AppendLine("Output your ENTIRE review as your FINAL MESSAGE (the runner captures it to a file). Do not write it to disk yourself.")
    [void]$sb.AppendLine("End the review with this EXACT line and nothing after it:")
    [void]$sb.AppendLine((Get-ReviewSentinel $n))
    [void]$sb.AppendLine(("If you truly cannot complete, make your final message begin 'BATCH_HALT phase={0} reason='." -f $n))
    [void]$sb.AppendLine(""); [void]$sb.AppendLine("===== BATCH PROMPT ====="); [void]$sb.AppendLine($PromptBody)
    [void]$sb.AppendLine(""); [void]$sb.AppendLine(("===== PHASE {0} DIFF UNDER REVIEW =====" -f $n)); [void]$sb.AppendLine((Get-PhaseDiffText $n))
    return $sb.ToString()
}
function Build-AdjudicatePrompt($n){
    $rf=Get-ReviewFile $n
    $meta=Read-StepMetadata $rf
    $prov=if($meta){ (" reviewer_engine={0}; requested_model={1}; actual_models={2}; effort={3}; cli_version={4}" -f $meta.engine,$meta.requested_model,((@($meta.actual_models)) -join ','),$meta.requested_effort,$meta.cli_version) } else { " (provenance unavailable)" }
    $sb=New-Object System.Text.StringBuilder
    [void]$sb.AppendLine(("AUTOMATED BATCH - single step. You are the AUTHOR for Phase {0}, step C (ADJUDICATE AND MERGE)." -f $n))
    [void]$sb.AppendLine("You wrote this phase. An opposing-family reviewer interrogated your diff; its review is inlined below. Your context is fresh, so re-read $PromptFile and re-read your own diff from git before judging anything.")
    [void]$sb.AppendLine("YOU HAVE FINAL SAY. For EVERY finding, use the 'Adjudication schema' in the BATCH PROMPT and decide exactly one of:")
    [void]$sb.AppendLine("  ACCEPTED-FIXED         -- you agree; fix it properly (not a band-aid), with a test that covers it.")
    [void]$sb.AppendLine("  REJECTED               -- you disagree; give an evidence-based rationale with file:line PROVING the reviewer is wrong. 'I prefer my version' is NOT a rationale. A cross-family reviewer produces real bugs AND confident false positives -- do not cave to a confident tone, and do not dismiss a finding you cannot actually refute.")
    [void]$sb.AppendLine("  DEFERRED-OUT-OF-SCOPE  -- real, but in code this phase did not touch. Do NOT fix it here (scope creep breaks the phase contract); record it for the final report.")
    [void]$sb.AppendLine("MUTATION EVIDENCE IS MANDATORY for every test-theater and test-gap finding. Do not argue about these -- run the experiment. Apply the reviewer's named single-line mutation to the implementation, run that specific test, record the observed outcome, then REVERT the mutation.")
    [void]$sb.AppendLine("  - The test FAILED under the mutation  -> the test is real; you may REJECT the finding, quoting the failure line as your evidence.")
    [void]$sb.AppendLine("  - The test still PASSED under the mutation -> the reviewer is proven right and the test is theater; you may NOT reject it. Fix the test so it genuinely fails under that mutation, revert the mutation, and confirm it passes again on correct code.")
    [void]$sb.AppendLine("You may NOT reject a test-theater finding on argument alone: rejection REQUIRES the demonstrated failure. A test that cannot fail is worse than no test, and you wrote it, so you are the one who has to prove it can.")
    [void]$sb.AppendLine("ABSOLUTE: a mutation is temporary scaffolding. Revert every one, confirm `git diff` shows ZERO mutation residue, and never commit a mutation. The FULL suite must be green on UNMUTATED code.")
    [void]$sb.AppendLine("Then: re-run the FULL comprehensive suite on unmutated code (5-attempt ultrathink protocol if it fails), commit your fixes on the phase branch, and MERGE the phase branch --no-ff into $BaseBranch. Do NOT push.")
    [void]$sb.AppendLine("If the suite cannot go green, do NOT merge: make your FINAL MESSAGE begin 'BATCH_HALT phase=$n reason=' and stop.")
    [void]$sb.AppendLine("Your FINAL MESSAGE must be the complete adjudication record (the runner captures it). End it with this EXACT line and nothing after it:")
    [void]$sb.AppendLine((Get-AdjSentinel $n))
    [void]$sb.AppendLine(""); [void]$sb.AppendLine("===== BATCH PROMPT ====="); [void]$sb.AppendLine($PromptBody)
    [void]$sb.AppendLine(""); [void]$sb.AppendLine(("===== REVIEW OF PHASE {0};{1} =====" -f $n,$prov))
    [void]$sb.AppendLine((Get-Content $rf -Raw))
    return $sb.ToString()
}

# ---- generic step runner. Returns 'OK' | 'HALT' | 'FAILED' ----
function Test-HaltOutput($file){
    if(-not (Test-Path $file)){ return $false }
    return (Select-String -Path $file -Pattern '^BATCH_HALT phase=' -Quiet)
}
function Run-Step($step){
    # $step: N, Name, Engine, Model, Effort, Write, Prompt, Resume, Out, Sentinel (or $null), Verify (or $null)
    $deadline=(Get-Date).AddHours($MaxStepElapsedHours)
    $sid=if($step.Engine -eq 'claude'){ [guid]::NewGuid().ToString() } else { $null }
    $sessionStarted=$false
    foreach($p in @($step.Out,(Get-MetaPath $step.Out))){ if(Test-Path $p){ Remove-Item $p -Force } }
    for($a=1;$a -le $MaxAttemptsPerPhase;$a++){
        if((Get-Date) -ge $deadline){ $msg=("Phase {0} step {1} exceeded {2}h." -f $step.N,$step.Name,$MaxStepElapsedHours); Log ("HALT: {0}" -f $msg); Write-Halt $step.N $msg; return 'HALT' }
        $ready=Resolve-UsableModel $step.Engine $step.Model $deadline
        if($ready.Status -ne 'READY'){
            $msg=("Phase {0} step {1} {2}/{3} readiness {4}: {5}" -f $step.N,$step.Name,$step.Engine,$step.Model,$ready.Status,$ready.Detail)
            Log ("HALT: {0}" -f $msg); Write-Halt $step.N $msg; return 'HALT'
        }
        $useModel=$ready.Model
        $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
        $rawLog=Join-Path $LogDir ("phase_{0:D2}_{1}_attempt{2}_{3}.log" -f $step.N,$step.Name,$a,$stamp)
        $mode=if($sessionStarted -and $sid -and (($step.Engine -eq 'claude') -or $CodexSupportsResume)){ 'resume' } else { 'fresh' }
        $remaining=[int][Math]::Max(1,($deadline-(Get-Date)).TotalSeconds)
        $timeout=[int][Math]::Min($InvocationTimeoutSeconds,$remaining)
        Log ("=== Phase {0} step {1} attempt {2}/{3} {4} ({5} on {6}, effort={7}, write={8}, watchdog={9}s)" -f $step.N,$step.Name,$a,$MaxAttemptsPerPhase,$mode.ToUpperInvariant(),$step.Engine,$useModel,$step.Effort,$step.Write,$timeout)
        $startedUtc=(Get-Date).ToUniversalTime().ToString('o')
        $text=if($mode -eq 'resume'){ $step.Resume } else { $step.Prompt }
        $result=Invoke-Engine $step.Engine $mode $text $useModel $step.Effort $step.Out $rawLog $sid $timeout $step.Write
        $completedUtc=(Get-Date).ToUniversalTime().ToString('o')
        if($result.SessionId){ $sid=[string]$result.SessionId }
        $sessionStarted=$true
        if(($mode -eq 'resume') -and (($null -eq $result.ExitCode) -or ([int]$result.ExitCode -ne 0)) -and (Test-NoConversation $rawLog)){
            $sid=if($step.Engine -eq 'claude'){ [guid]::NewGuid().ToString() } else { $null }
            Log '    resume session was unavailable; restarting this attempt FRESH.'
            $rawLog=Join-Path $LogDir ("phase_{0:D2}_{1}_attempt{2}b_{3}.log" -f $step.N,$step.Name,$a,$stamp)
            $startedUtc=(Get-Date).ToUniversalTime().ToString('o')
            $result=Invoke-Engine $step.Engine 'fresh' $step.Prompt $useModel $step.Effort $step.Out $rawLog $sid $timeout $step.Write
            $completedUtc=(Get-Date).ToUniversalTime().ToString('o')
            if($result.SessionId){ $sid=[string]$result.SessionId }
        }
        Log ("=== Phase {0} step {1} attempt {2} returned (exit={3}, timeout={4}, actual_models={5})" -f $step.N,$step.Name,$a,$result.ExitCode,$result.TimedOut,(@($result.ActualModels)-join ','))
        if(Halted $step.N){ return 'HALT' }
        if(Test-HaltOutput $step.Out){ $reason=[IO.File]::ReadAllText($step.Out); Write-Halt $step.N $reason; Log ("HALT: model halted phase {0} at step {1}." -f $step.N,$step.Name); return 'HALT' }
        $clean=(($null -ne $result.ExitCode) -and ([int]$result.ExitCode -eq 0) -and -not $result.TimedOut -and -not $result.StartError)
        $ok=$clean
        if($ok -and $step.Sentinel){ $ok=(Test-SentinelComplete $step.Out $step.Sentinel) }
        if($ok -and $step.Verify){ $ok=[bool](& $step.Verify) }
        if($ok){
            if(Test-Path $step.Out){
                Write-StepMetadata $step $result $useModel $a $startedUtc $completedUtc
                if(-not (Test-RequestedModelObserved $useModel $result.ActualModels)){ Log ("WARNING: requested model {0}; actual model(s) {1}. The sidecar records the difference (see the Fable reroute trap)." -f $useModel,(@($result.ActualModels)-join ',')) }
            } else { Log ("WARNING: phase {0} step {1} passed its gate but produced no captured record." -f $step.N,$step.Name) }
            Log ("=== Phase {0} step {1} OK." -f $step.N,$step.Name); return 'OK'
        }
        $class=Get-FailureClass $result $step.Engine
        if($class -eq 'FATAL'){
            $msg=("Phase {0} step {1} fatal {2} failure: {3}" -f $step.N,$step.Name,$step.Engine,(Get-FailureTail $rawLog))
            Log ("HALT: {0}" -f $msg); Write-Halt $step.N $msg; return 'HALT'
        }
        if($clean){ $class='INCOMPLETE' }
        Log ("    attempt {0} classification={1}; step failed its sentinel/verify gate." -f $a,$class)
        if($a -lt $MaxAttemptsPerPhase){ Start-Sleep -Seconds $RetryPauseSeconds }
    }
    $msg=("Phase {0} step {1} did not complete after {2} attempts. See batch_logs." -f $step.N,$step.Name,$MaxAttemptsPerPhase)
    Log ("HALT: {0}" -f $msg); Write-Halt $step.N $msg; return 'FAILED'
}

function Test-RejectedCritical($n){
    $f=Get-AdjFile $n
    if(-not (Test-Path $f)){ return $false }
    $text=[IO.File]::ReadAllText($f)
    # Heuristic ledger scan: a REJECTED decision in the same block as a Critical severity.
    foreach($block in ($text -split '(?m)^\s*(?=Ref\s*[:=])')){
        if(($block -match '(?i)Decision\s*[:=]\s*REJECTED') -and ($block -match '(?i)Severity\s*[:=]\s*Critical')){ return $true }
    }
    return $false
}

# ---- one implementation phase: implement -> review -> adjudicate+merge ----
function Run-ImplPhase($n){
    if((-not $Fresh) -and (Test-PhaseComplete $n)){ Log ("=== Phase {0}: adjudicated and merged into {1}; SKIP." -f $n,$BaseBranch); return 'OK' }

    # --- step A: AUTHOR implements ---
    if($Fresh -or -not (Test-PhaseImplemented $n)){
        $stepA=[pscustomobject]@{
            N=$n; Name='impl'; Engine=$AuthorFamily; Model=$PhaseModel[$n]; Effort=$PhaseEffort[$n]; Write=$true
            Prompt=(Build-ImplPrompt $n); Resume=("AUTOMATED BATCH RESUME - Phase {0} step A (IMPLEMENT). Your previous session was interrupted. Inspect the repo state first ({1}{0}-* may exist with partial work), then continue exactly where you left off: finish the implementation, run comprehensive testing, and COMMIT on the phase branch. Do NOT merge and do NOT push. If prior partial work is unusable, reset the branch from {2} and redo it." -f $n,$BranchPrefix,$BaseBranch)
            Out=(Get-ImplFile $n); Sentinel=$null; Verify={ Test-PhaseImplemented $n }.GetNewClosure()
        }
        $r=Run-Step $stepA
        if($r -ne 'OK'){ return $r }
    } else { Log ("=== Phase {0} step impl: branch already ahead of {1}; SKIP to review." -f $n,$BaseBranch) }
    if(-not (Test-PhaseImplemented $n)){
        $msg=("Phase {0} implement step produced no commits ahead of {1}." -f $n,$BaseBranch)
        Log ("HALT: {0}" -f $msg); Write-Halt $n $msg; return 'HALT'
    }

    # --- step B: REVIEWER interrogates the diff (read-only, opposing family) ---
    $branch=Get-PhaseBranch $n
    & git checkout $branch 2>&1 | Out-Null
    $reviewFile=Get-ReviewFile $n
    if($Fresh -or -not (Test-StepArtifact $reviewFile (Get-ReviewSentinel $n))){
        $before=Get-RepoState
        $stepB=[pscustomobject]@{
            N=$n; Name='review'; Engine=$ReviewerFamily; Model=$PhaseReviewModel[$n]; Effort=$PhaseReviewEffort[$n]; Write=$false
            Prompt=(Build-ReviewPrompt $n); Resume=("AUTOMATED BATCH RESUME - Phase {0} step B (REVIEW). Continue, then RE-EMIT THE COMPLETE REVIEW FROM THE BEGINNING as your final message. Do not emit only a continuation. You remain STRICTLY READ-ONLY. End with exactly: {1}" -f $n,(Get-ReviewSentinel $n))
            Out=$reviewFile; Sentinel=(Get-ReviewSentinel $n); Verify=$null
        }
        $r=Run-Step $stepB
        if($r -ne 'OK'){ return $r }
        # Read-only tripwire: the reviewer must not have moved the tree.
        $after=Get-RepoState
        if(($after.Head -ne $before.Head) -or ($after.Dirty -ne $before.Dirty) -or ($after.Branch -ne $before.Branch)){
            $msg=("SAFETY: the reviewer changed the repo during a read-only step (HEAD/branch/tracked files moved) in phase {0}. Investigate before re-running." -f $n)
            Log ("HALT: {0}" -f $msg); Write-Halt $n $msg; return 'HALT'
        }
        if(-not (Test-StepArtifact $reviewFile (Get-ReviewSentinel $n))){
            $msg=("Phase {0} review record failed sentinel/provenance/hash validation." -f $n)
            Log ("HALT: {0}" -f $msg); Write-Halt $n $msg; return 'HALT'
        }
    } else { Log ("=== Phase {0} step review: valid review record present; SKIP." -f $n) }

    # --- step C: AUTHOR adjudicates every finding, re-tests, merges ---
    & git checkout $branch 2>&1 | Out-Null
    $baseBefore=((& git rev-parse $BaseBranch) | Select-Object -First 1)
    $stepC=[pscustomobject]@{
        N=$n; Name='adjudicate'; Engine=$AuthorFamily; Model=$PhaseModel[$n]; Effort=$PhaseEffort[$n]; Write=$true
        Prompt=(Build-AdjudicatePrompt $n); Resume=("AUTOMATED BATCH RESUME - Phase {0} step C (ADJUDICATE AND MERGE). Your previous session was interrupted. Re-read the review record at {1} and your own diff, finish adjudicating EVERY finding, re-run the full suite, then merge the phase branch --no-ff into {2}. Re-emit the COMPLETE adjudication record as your final message, ending with exactly: {3}" -f $n,(Get-ReviewFile $n),$BaseBranch,(Get-AdjSentinel $n))
        Out=(Get-AdjFile $n); Sentinel=$null
        Verify={ (((& git rev-parse $BaseBranch) | Select-Object -First 1) -ne $baseBefore) }.GetNewClosure()
    }
    $r=Run-Step $stepC
    if($r -ne 'OK'){ return $r }
    if(-not (Test-SentinelComplete (Get-AdjFile $n) (Get-AdjSentinel $n))){
        Log ("WARNING: phase {0} merged but its adjudication record has no end sentinel; the record may be truncated. Read it before trusting the ledger." -f $n)
    }
    if($HaltOnRejectedCritical -and (Test-RejectedCritical $n)){
        $msg=("Phase {0}: the author REJECTED a Critical review finding and HaltOnRejectedCritical is on. Read {1} and arbitrate." -f $n,(Get-AdjFile $n))
        Log ("HALT: {0}" -f $msg); Write-Halt $n $msg; return 'HALT'
    }
    return 'OK'
}

# ---- preflight ----
if(-not (Get-Command git -ErrorAction SilentlyContinue)){ Log "ABORT: 'git' not on PATH."; exit 1 }
if(($AuthorFamily -eq 'claude') -or ($ReviewerFamily -eq 'claude')){
    if(-not (Get-Command claude -ErrorAction SilentlyContinue)){ Log "ABORT: 'claude' not on PATH."; exit 1 }
}
if(($AuthorFamily -eq 'codex') -or ($ReviewerFamily -eq 'codex')){
    if(-not (Get-Command codex -ErrorAction SilentlyContinue)){ Log "ABORT: 'codex' not on PATH."; exit 1 }
}
if(-not (Test-Path (Join-Path $Repo $PromptFile))){ Log ("ABORT: prompt file missing: {0}" -f $PromptFile); exit 1 }

# THE OPPOSITION INVARIANT + config sanity. A same-family review is worth almost nothing, so refuse to run.
if($AuthorFamily -eq $ReviewerFamily){ Log ("ABORT: AuthorFamily and ReviewerFamily are both '{0}'. A same-family review shares the author's blind spots -- that is the one thing V2 exists to prevent." -f $AuthorFamily); exit 1 }
for($i=1;$i -le $FinalPhase;$i++){
    if(-not $PhaseModel.ContainsKey($i)){ Log ("ABORT: `$PhaseModel has no entry for phase {0}." -f $i); exit 1 }
    if(-not $PhaseEffort.ContainsKey($i)){ Log ("ABORT: `$PhaseEffort has no entry for phase {0}." -f $i); exit 1 }
    $m=[string]$PhaseModel[$i]
    if([string]$ModelFamily[$m] -ne $AuthorFamily){ Log ("ABORT: phase {0} author model '{1}' is not in the author family '{2}'." -f $i,$m,$AuthorFamily); exit 1 }
}
for($i=1;$i -le $NumImplPhases;$i++){
    if(-not $PhaseReviewModel.ContainsKey($i)){ Log ("ABORT: `$PhaseReviewModel has no entry for phase {0}." -f $i); exit 1 }
    if(-not $PhaseReviewEffort.ContainsKey($i)){ Log ("ABORT: `$PhaseReviewEffort has no entry for phase {0}." -f $i); exit 1 }
    $rm=[string]$PhaseReviewModel[$i]
    if([string]$ModelFamily[$rm] -ne $ReviewerFamily){ Log ("ABORT: phase {0} reviewer model '{1}' is not in the reviewer family '{2}'." -f $i,$rm,$ReviewerFamily); exit 1 }
}
foreach($fam in @($FamilyFallback.Keys)){
    $fb=[string]$FamilyFallback[$fam]
    if($fb -and ([string]$ModelFamily[$fb] -ne $fam)){ Log ("ABORT: FamilyFallback['{0}'] = '{1}' is not in family '{0}'. A cross-family fallback would silently collapse the author/reviewer opposition." -f $fam,$fb); exit 1 }
}
foreach($sm in @($SmokeModelSubstitute.Keys)){
    $sub=[string]$SmokeModelSubstitute[$sm]
    if($sub -and ([string]$ModelFamily[$sub] -ne [string]$ModelFamily[$sm])){ Log ("ABORT: SmokeModelSubstitute['{0}'] = '{1}' is a different model family. The smoke would drive the wrong engine adapter and prove nothing about '{0}'." -f $sm,$sub); exit 1 }
}
if($PushToOrigin -and ($AuthorFamily -eq 'codex') -and (-not $CodexNetworkAccess)){
    Log "ABORT: PushToOrigin is on with a codex author, but CodexNetworkAccess is off. codex --sandbox workspace-write blocks network, so the push WILL fail. Set `$CodexNetworkAccess = `$true or push manually."; exit 1
}

$ClaudeCliVersion=''; $CodexCliVersion=''
if(Get-Command claude -ErrorAction SilentlyContinue){ $ClaudeCliVersion=((& claude --version 2>&1 | Select-Object -First 1) -join '') }
if(Get-Command codex  -ErrorAction SilentlyContinue){ $CodexCliVersion=((& codex --version 2>&1 | Select-Object -First 1) -join '') }

# Swap a premium model for its cheap same-family stand-in during -SmokeTest. -SmokeFull disables this.
$SmokeSubNotes=@()
function Resolve-SmokeModel($model){
    $m=[string]$model
    if($SmokeFull){ return $m }
    $sub=[string]$SmokeModelSubstitute[$m]
    if($sub -and ($sub -ne $m)){ $script:SmokeSubNotes += ("{0} -> {1}" -f $m,$sub); return $sub }
    return $m
}

if($SmokeTest){
    Log 'SMOKE TEST: real STDIN -> timed adapter -> capture -> exact sentinel -> provenance sidecar, for every tuple; plus a real author WRITE + git probe.'
    $ok=$true; $i=0
    $tuples=@()
    for($p=1;$p -le $FinalPhase;$p++){ $tuples += ("{0}|{1}|{2}" -f $AuthorFamily,(Resolve-SmokeModel $PhaseModel[$p]),$PhaseEffort[$p]) }
    for($p=1;$p -le $NumImplPhases;$p++){ $tuples += ("{0}|{1}|{2}" -f $ReviewerFamily,(Resolve-SmokeModel $PhaseReviewModel[$p]),$PhaseReviewEffort[$p]) }
    # Fallback models are always smoked for real: if a premium model is capped at runtime, THIS is the model
    # that actually carries the phase, so it is the one that must be proven working.
    foreach($fam in @($FamilyFallback.Keys)){ if($FamilyFallback[$fam]){ $tuples += ("{0}|{1}|low" -f $fam,$FamilyFallback[$fam]) } }
    if($SmokeFull){ Log '  -SmokeFull: substitutions disabled; smoking the REAL configured models (this burns premium usage).' }
    elseif($SmokeSubNotes.Count -gt 0){
        Log ("  USAGE SAVING: smoke substituted {0}. Those models were NOT verified here." -f ((@($SmokeSubNotes | Sort-Object -Unique)) -join '; '))
        Log '  That is by design: the stand-in is the same model FamilyFallback uses on a cap, so the smoke proves the path a cap would take. Run -SmokeTest -SmokeFull to prove the premium model itself is reachable.'
    }
    foreach($tuple in (@($tuples) | Sort-Object -Unique)){
        $i++
        $eng,$mdl,$eff=$tuple -split '\|',3
        $sentinel=("<!-- BATCH_SMOKE_{0}_COMPLETE name={1} -->" -f (900+$i),$Name)
        $safeModel=$mdl -replace '[^A-Za-z0-9]','_'
        $out=Join-Path $LogDir ("smoke_{0}_{1}_{2}_{3}.md" -f $eng,$safeModel,$eff,$runStamp)
        $raw=Join-Path $LogDir ("smoke_{0}_{1}_{2}_{3}.log" -f $eng,$safeModel,$eff,$runStamp)
        foreach($p in @($out,(Get-MetaPath $out))){ if(Test-Path $p){ Remove-Item $p -Force } }
        $smokePrompt=("READ-ONLY ADAPTER SMOKE TEST. Do not use tools and do not write files. Emit a complete Markdown report of at least 250 characters with headings `Smoke purpose`, `Transport`, and `Result`. State that the prompt arrived through STDIN, name the requested engine/model/effort ({0}/{1}/{2}), include two full explanatory sentences, then end with this exact line and nothing after it:`n{3}" -f $eng,$mdl,$eff,$sentinel)
        $sid=if($eng -eq 'claude'){ [guid]::NewGuid().ToString() } else { $null }
        $started=(Get-Date).ToUniversalTime().ToString('o')
        $result=Invoke-Engine $eng 'fresh' $smokePrompt $mdl $eff $out $raw $sid $SmokeTimeoutSeconds $false
        $completed=(Get-Date).ToUniversalTime().ToString('o')
        $actual=@($result.ActualModels)
        $modelObserved=Test-RequestedModelObserved $mdl $actual
        $good=(($null -ne $result.ExitCode) -and ([int]$result.ExitCode -eq 0) -and -not $result.TimedOut -and -not $result.StartError -and (Test-SentinelComplete $out $sentinel) -and $modelObserved)
        if($good){
            $sp=[pscustomobject]@{ N=(900+$i); Name='smoke'; Engine=$eng; Model=$mdl; Effort=$eff; Write=$false; Out=$out }
            Write-StepMetadata $sp $result $mdl 1 $started $completed
            $good=Test-StepArtifact $out $sentinel
        }
        if($good){ Log ("  PASS {0}/{1}/effort={2}; actual={3}; output+metadata verified." -f $eng,$mdl,$eff,($actual-join ',')) }
        else {
            $ok=$false; $class=Get-FailureClass $result $eng
            if(($null -ne $result.ExitCode) -and ([int]$result.ExitCode -eq 0) -and -not $modelObserved){ $class='MODEL_PROVENANCE' }
            elseif(($null -ne $result.ExitCode) -and ([int]$result.ExitCode -eq 0)){ $class='INCOMPLETE' }
            Log ("  FAIL {0}/{1}/effort={2}; exit={3}; timeout={4}; class={5}; actual={6}; tail={7}" -f $eng,$mdl,$eff,$result.ExitCode,$result.TimedOut,$class,($actual-join ','),(Get-FailureTail $raw))
        }
    }
    # The one thing a read-only smoke can never prove: that the AUTHOR can actually write and drive git.
    Log 'SMOKE TEST: author write + git capability probe (the runner deletes the probe file afterwards).'
    $probeRel=("batch_logs/.write_probe_{0}.txt" -f $runStamp)
    $probeAbs=Join-Path $Repo $probeRel
    if(Test-Path $probeAbs){ Remove-Item $probeAbs -Force }
    $wOut=Join-Path $LogDir ("smoke_writeprobe_{0}.md" -f $runStamp)
    $wRaw=Join-Path $LogDir ("smoke_writeprobe_{0}.log" -f $runStamp)
    $wPrompt=("AUTHOR WRITE CAPABILITY PROBE. Do exactly this and nothing else. Create the file '{0}' (relative to the repo root) containing exactly two lines: line 1 is WRITE-OK, line 2 is the output of `git rev-parse --abbrev-ref HEAD`. Do not modify any other file, do not commit, do not create branches. Reply with a one-line confirmation." -f $probeRel)
    # Substituted too: this probe does REAL tool work (write + git), so it is the priciest smoke call, and
    # write capability is a property of the CLI/sandbox posture, not of which model in the family is driving.
    $wModel=Resolve-SmokeModel $PhaseModel[1]
    $wSid=if($AuthorFamily -eq 'claude'){ [guid]::NewGuid().ToString() } else { $null }
    $wRes=Invoke-Engine $AuthorFamily 'fresh' $wPrompt $wModel $PhaseEffort[1] $wOut $wRaw $wSid $SmokeTimeoutSeconds $true
    $expectBranch=((& git rev-parse --abbrev-ref HEAD) | Select-Object -First 1)
    $wGood=$false
    if(Test-Path $probeAbs){
        $probeText=[IO.File]::ReadAllText($probeAbs)
        $wGood=(($probeText -match '(?m)^\s*WRITE-OK\s*$') -and ($probeText -match [regex]::Escape($expectBranch)))
        Remove-Item $probeAbs -Force
    }
    if($wGood){ Log ("  PASS author write+git probe ({0} on {1}): file written and `git rev-parse` ran inside the sandbox." -f $AuthorFamily,$wModel) }
    else {
        $ok=$false
        Log ("  FAIL author write+git probe ({0} on {1}); exit={2}; tail={3}" -f $AuthorFamily,$wModel,$wRes.ExitCode,(Get-FailureTail $wRaw))
        if($AuthorFamily -eq 'claude'){ Log '    Likely: --dangerously-skip-permissions was never accepted interactively (run `claude --dangerously-skip-permissions` once), auth expired (claude login), or a usage limit.' }
        else { Log '    Likely: codex --sandbox workspace-write cannot write or cannot run git here, approvals are not set to never, or auth expired (codex login).' }
    }
    $sd=(& git status --porcelain --untracked-files=no)
    if($sd){ Log "WARNING: modified tracked files; the real run ABORTS until they are committed or stashed:"; $sd | %{ Log ("    "+$_) } }
    if($ok){ Log 'SMOKE TEST PASSED: every engine/model/effort tuple works and the author can write + drive git.' ; exit 0 }
    Log 'SMOKE TEST FAILED: fix the reported issue before the overnight run.'; exit 1
}

$staleHalts = Get-ChildItem -Path $Repo -Filter "BATCH_HALT_phase_*.md" -ErrorAction SilentlyContinue
if($staleHalts){ Log "ABORT: stale halt file(s) exist. Read then delete to proceed:"; $staleHalts | %{ Log ("    "+$_.Name) }; exit 1 }
$dirty = (& git status --porcelain --untracked-files=no)
if($dirty){ Log "ABORT: modified tracked files would contaminate phase branches. Commit or stash first:"; $dirty | %{ Log ("    "+$_) }; exit 1 }

# The author commits code while the runner writes records INSIDE the repo. A stray `git add -A` would drag
# runner artifacts into the project history, so warn loudly if they are not ignored.
foreach($d in @($RecordsDir,'batch_logs')){
    & git check-ignore -q $d 2>$null | Out-Null
    if($LASTEXITCODE -ne 0){ Log ("WARNING: '{0}/' is NOT gitignored. The author engine may commit runner artifacts into your history. Add it to .gitignore before an unattended run." -f $d) }
}

Log ("Batch start. repo={0}" -f $Repo)
Log ("AUTHOR family={0}; REVIEWER family={1} (opposition asserted). PushToOrigin={2}" -f $AuthorFamily,$ReviewerFamily,$PushToOrigin)
Log ("CLI versions: claude={0}; codex={1}" -f $ClaudeCliVersion,$CodexCliVersion)
Log "REMINDER: this run assumes the human gate after Run A was honored (CDX-002 rebuild + runtime==image verification)."
for($n=1;$n -le $NumImplPhases;$n++){ Log ("  phase {0}: author {1}/{2} effort={3} -> review {4}/{5} effort={6}" -f $n,$AuthorFamily,$PhaseModel[$n],$PhaseEffort[$n],$ReviewerFamily,$PhaseReviewModel[$n],$PhaseReviewEffort[$n]) }
Log ("  phase {0}: finalization {1}/{2} effort={3} (no review)" -f $FinalPhase,$AuthorFamily,$PhaseModel[$FinalPhase],$PhaseEffort[$FinalPhase])

# ---- ensure base branch exists. HARDENED for this run: nonkyc is the pinned review base and must NEVER
# be created or fast-forwarded by the runner. If it is missing, something is very wrong -- ABORT. ----
& git rev-parse --verify $BaseBranch 2>$null | Out-Null
if($LASTEXITCODE -ne 0){
    if($BaseCreateFrom -eq ""){ Log ("ABORT: base branch '{0}' does not exist and this run must never create it. Investigate the repo before re-running." -f $BaseBranch); exit 1 }
    $from=$BaseCreateFrom
    & git rev-parse --verify $from 2>$null | Out-Null
    if($LASTEXITCODE -ne 0){ $from="master" }
    Log ("Creating {0} from {1}..." -f $BaseBranch,$from); & git checkout -b $BaseBranch $from 2>&1 | Out-Null
} else { & git checkout $BaseBranch 2>&1 | Out-Null }
if($SyncBaseFrom -ne ""){
    Log ("Syncing {0} from {1} (ff-only)..." -f $BaseBranch,$SyncBaseFrom)
    & git merge --ff-only $SyncBaseFrom 2>&1 | Tee-Object -FilePath (Join-Path $LogDir ("sync_{0}.log" -f $runStamp)) | Out-Null
    if($LASTEXITCODE -ne 0){ Log ("ABORT: {0} sync from {1} is not a fast-forward." -f $BaseBranch,$SyncBaseFrom); exit 1 }
}

# ---- implementation phases (implement -> review -> adjudicate+merge) ----
for($n=1;$n -le $NumImplPhases;$n++){
    $r=Run-ImplPhase $n
    if($r -ne 'OK'){ Log ("Batch stopped at phase {0} ({1})." -f $n,$r); exit 2 }
    & git checkout $BaseBranch 2>&1 | Out-Null
    Log ("=== Phase {0} merged to {1} ({2})." -f $n,$BaseBranch,((& git rev-parse $BaseBranch) | Select-Object -First 1))
}

# ---- finalization phase (author engine; no cross-review) ----
& git checkout $BaseBranch 2>&1 | Out-Null
if($IntegrationBranch -ne ""){
    if($PushToOrigin){ $intClause=("merge {0} into {1}, then push {0} and {1} to origin" -f $BaseBranch,$IntegrationBranch) }
    else             { $intClause=("merge {0} into {1} (do NOT push)" -f $BaseBranch,$IntegrationBranch) }
} else {
    if($PushToOrigin){ $intClause=("push {0} to origin" -f $BaseBranch) } else { $intClause=("leave everything on {0} (no merge, no push)" -f $BaseBranch) }
}
$finalPrompt=@"
AUTOMATED BATCH - finalization (Phase $FinalPhase). Read $PromptFile. Run full comprehensive testing on $BaseBranch.
If green, $intClause.
Then write the final dense report to $ReportFile in the repo root. It MUST include a REVIEW LEDGER built by
reading every record in $RecordsDir/: for each phase, the findings the opposing-family reviewer raised and how
the author adjudicated them (accepted+fixed / rejected + the rationale / deferred out-of-scope). Add a
prominent 'REQUIRES HUMAN ARBITRATION' section listing every REJECTED Critical/High finding and every
DEFERRED finding, each with enough detail for a human to settle it without re-reading the raw records.
It MUST also include a TEST INTEGRITY subsection: every test-theater finding, and for each, whether the
author's mutation experiment showed the test FAILING under the mutation (the test is real) or still PASSING
(it was theater and was then fixed). Any test-theater finding that was REJECTED without mutation evidence is
a protocol violation -- list it under REQUIRES HUMAN ARBITRATION, because it means a test that may be
incapable of failing is still sitting in the suite being counted as coverage.
If testing fails, make your FINAL MESSAGE begin 'BATCH_HALT phase=$FinalPhase reason=' and stop WITHOUT the
integration merge.
"@
$finalOut=Join-Path $RecordsPath ("FINALIZATION_{0}.md" -f $Name)
if($IntegrationBranch -ne ""){ $verifyF={ & git merge-base --is-ancestor $BaseBranch $IntegrationBranch 2>$null | Out-Null; $LASTEXITCODE -eq 0 }.GetNewClosure() }
else                         { $verifyF={ Test-Path (Join-Path $Repo $ReportFile) }.GetNewClosure() }
$stepF=[pscustomobject]@{
    N=$FinalPhase; Name='finalize'; Engine=$AuthorFamily; Model=$PhaseModel[$FinalPhase]; Effort=$PhaseEffort[$FinalPhase]; Write=$true
    Prompt=$finalPrompt; Resume=("AUTOMATED BATCH RESUME - finalization (Phase {0}). Your previous session was interrupted. Re-read {1}, re-run full comprehensive testing on {2}, then complete the finalization exactly as specified, including the REVIEW LEDGER built from {3}/." -f $FinalPhase,$PromptFile,$BaseBranch,$RecordsDir)
    Out=$finalOut; Sentinel=$null; Verify=$verifyF
}
$rF=Run-Step $stepF
if($rF -ne 'OK'){ Log ("Batch stopped at finalization ({0})." -f $rF); exit 2 }
Log ("=== BATCH COMPLETE. Read {0} first (its REVIEW LEDGER + REQUIRES HUMAN ARBITRATION sections), then {1}/ and batch_logs. OPEN OBLIGATION: the live resume-preview dry-run against a real stopped instance is yours, by hand." -f $ReportFile,$RecordsDir)
exit 0
