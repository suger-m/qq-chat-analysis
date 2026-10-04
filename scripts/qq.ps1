# Native Windows wrapper. Do not consume the CLI's raw -o arguments.
$CliArgs = @($args)
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot 'qq.py'
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw 'Incomplete skill package: scripts/qq.py is missing.'
}
if ($CliArgs.Count -eq 1 -and $CliArgs[0] -eq '-PythonPath') {
    $CliArgs = @('--python-path')
}

function Find-UsablePython([string]$Candidate) {
    if (-not $Candidate) { return $null }
    $resolved = if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        (Resolve-Path -LiteralPath $Candidate).Path
    } else {
        $command = Get-Command $Candidate -CommandType Application -ErrorAction SilentlyContinue
        if ($command) { $command.Source } else { $null }
    }
    if (-not $resolved) { return $null }
    $probe = 'import sys; assert sys.version_info >= (3,10); import blackboxprotobuf, cryptography; print(sys.executable)'
    # Windows Store aliases and missing modules may write stderr; probe failure is expected.
    $priorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $result = & $resolved -c $probe 2>$null
        if ($LASTEXITCODE -eq 0) { return [string]($result | Select-Object -Last 1) }
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $priorPreference
    }
    return $null
}

$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    if ($env:QQ_PY) {
        $pythonExe = Find-UsablePython $env:QQ_PY
        if (-not $pythonExe) {
            throw 'QQ_PY must select Python 3.10+ with the core dependencies. Install scripts/runtime/requirements.txt into that environment.'
        }
        & $pythonExe $entry @CliArgs
    } else {
        $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source run --locked --script $entry @CliArgs
        } else {
            $pythonExe = $null
            foreach ($name in @('python', 'python3')) {
                $pythonExe = Find-UsablePython $name
                if ($pythonExe) { break }
            }
            if (-not $pythonExe) {
                $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
                if ($launcher) {
                    $priorPreference = $ErrorActionPreference
                    try {
                        $ErrorActionPreference = 'Continue'
                        $launched = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
                        if ($LASTEXITCODE -eq 0) {
                            $pythonExe = Find-UsablePython ([string]($launched | Select-Object -Last 1))
                        }
                    } finally { $ErrorActionPreference = $priorPreference }
                }
            }
            if (-not $pythonExe) {
                throw 'Install uv, or select Python 3.10+ with QQ_PY and install scripts/runtime/requirements.txt. See references/install.md.'
            }
            & $pythonExe $entry @CliArgs
        }
    }
    $cliExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
}
exit $cliExitCode
