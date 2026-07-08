param(
    [string]$BuildDir = "build",
    [string]$Config = "release"
)

$ErrorActionPreference = "Stop"

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$NormalizedConfig = $Config.ToLowerInvariant()

& $Python tools\package.py test --build-dir $BuildDir --config $NormalizedConfig
