#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PythonPath,
    [string[]]$RemoteAddress = @('Any'),
    [switch]$SkipFirewall,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
$bundle = $PSScriptRoot
$manifest = Join-Path $bundle 'SHA256SUMS.txt'
$verified = @{}
foreach ($line in Get-Content -LiteralPath $manifest) {
    if ($line -notmatch '^([a-fA-F0-9]{64})  ((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)$') {
        throw "Invalid checksum entry in $manifest"
    }
    $expected = $Matches[1]
    $relativePath = $Matches[2]
    if ($verified.ContainsKey($relativePath) -or ($relativePath -split '/') -contains '..') {
        throw "Invalid bundle path $relativePath"
    }
    $file = Join-Path $bundle $relativePath
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash -ne $expected) {
        throw "Checksum mismatch for $relativePath"
    }
    $verified[$relativePath] = $true
}
foreach ($required in 'app/deploy/windows/PlateLog.xml', 'app/platelog/__main__.py', 'requirements.offline.txt', 'vendor/python-3.13.17-amd64.exe', 'vendor/WinSW.NET4.exe') {
    if (-not $verified.ContainsKey($required)) { throw "Missing checksum for $required" }
}
Write-Host "Verified $($verified.Count) bundle files."
if ($VerifyOnly) { return }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as Administrator to install PlateLog.'
}
if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Run Setup.ps1 from 64-bit PowerShell on an x64 Windows server.'
}
$serviceName = 'PlateLog'
$ruleName = 'PlateLog-Preview-TCP-8765'
$port = 8765
$app = Join-Path $env:ProgramFiles $serviceName
$root = Join-Path $env:ProgramData $serviceName
$data = Join-Path $root 'data'
$logs = Join-Path $root 'logs'
$wrapper = Join-Path $app 'PlateLog.exe'
$config = Join-Path $app 'PlateLog.xml'
$venv = Join-Path $app '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$pythonDirectory = Join-Path $env:ProgramFiles 'Python313'
$python = if ($PythonPath) { (Resolve-Path -LiteralPath $PythonPath).Path } else { Join-Path $pythonDirectory 'python.exe' }

if ((Test-Path -LiteralPath $app) -or (Test-Path -LiteralPath $root) -or (Get-Service $serviceName -ErrorAction SilentlyContinue)) {
    throw 'PlateLog already exists. Setup is for a new installation.'
}
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw "TCP port $port is already in use."
}
if (-not $SkipFirewall -and (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
    throw "The firewall rule $ruleName already exists. Use -SkipFirewall if IT manages it."
}
if ($null -eq (Get-ItemPropertyValue -LiteralPath 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release -ErrorAction SilentlyContinue)) {
    throw 'WinSW requires the .NET Framework 4.x Windows component.'
}

$installedService = $false
$createdApp = $false
$createdRoot = $false
$priorPipConfig = $env:PIP_CONFIG_FILE
try {
    New-Item -ItemType Directory -Path $app | Out-Null
    $createdApp = $true

    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Write-Host 'Installing bundled Python 3.13 x64 offline...'
        $installer = Join-Path $bundle 'vendor\python-3.13.17-amd64.exe'
        $pythonOptions = @(
            '/quiet', 'InstallAllUsers=1', 'Include_pip=1', 'Include_launcher=0', 'Include_test=0',
            'Include_doc=0', 'Include_tcltk=0', 'Include_tools=0', 'Shortcuts=0', 'PrependPath=0',
            ('TargetDir="{0}"' -f $pythonDirectory)
        )
        $setup = Start-Process -FilePath $installer -ArgumentList $pythonOptions -WindowStyle Hidden -Wait -PassThru
        if ($setup.ExitCode -in 1641,3010) { throw 'Python setup requires a restart. Restart Windows and run Setup.ps1 again.' }
        if ($setup.ExitCode -ne 0) { throw "Python setup failed ($($setup.ExitCode))." }
    }
    & $python -E -I -c "import sys, sysconfig; sys.exit(not (sys.version_info[:2] == (3, 13) and sysconfig.get_platform() == 'win-amd64' and sys.prefix == sys.base_prefix))"
    if ($LASTEXITCODE -ne 0) { throw 'Select a machine-wide x64 Python 3.13 installation with -PythonPath.' }

    foreach ($relativePath in $verified.Keys | Where-Object { $_.StartsWith('app/platelog/') }) {
        $target = Join-Path $app $relativePath.Substring(4)
        [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $bundle $relativePath) -Destination $target
    }
    Copy-Item -LiteralPath (Join-Path $bundle 'app\deploy\windows\PlateLog.xml') -Destination $config
    Copy-Item -LiteralPath (Join-Path $bundle 'vendor\WinSW.NET4.exe') -Destination $wrapper
    & $python -I -m venv --copies $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the PlateLog Python environment.' }
    $env:PIP_CONFIG_FILE = 'nul'
    & $venvPython -I -m pip --isolated --disable-pip-version-check install --no-index --no-cache-dir --only-binary=:all: --require-hashes --find-links (Join-Path $bundle 'wheels') -r (Join-Path $bundle 'requirements.offline.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the bundled Python dependencies.' }
    & $venvPython -I -m pip --isolated --disable-pip-version-check check
    if ($LASTEXITCODE -ne 0) { throw 'The bundled Python dependencies failed pip check.' }

    New-Item -ItemType Directory -Path $root | Out-Null
    $createdRoot = $true
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M'
    if ($LASTEXITCODE -ne 0) { throw 'Could not set PlateLog storage permissions.' }
    New-Item -ItemType Directory -Path $data,$logs | Out-Null

    Push-Location -LiteralPath $app
    try {
        & $wrapper install
        if ($LASTEXITCODE -ne 0) { throw 'Could not install the PlateLog Windows service.' }
        $installedService = $true
    } finally {
        Pop-Location
    }

    Start-Service -Name $serviceName
    $ready = $null
    $deadline = (Get-Date).AddSeconds(30)
    do {
        try {
            $ready = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 2
            if ($ready.status -eq 'ready' -and $ready.mode -eq 'synthetic-preview') { break }
        } catch {
            $ready = $null
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($ready.status -ne 'ready' -or $ready.mode -ne 'synthetic-preview') {
        Get-ChildItem -LiteralPath $logs -Filter '*.log' -File | ForEach-Object {
            Write-Warning $_.Name
            Get-Content -LiteralPath $_.FullName -Tail 12 | ForEach-Object { Write-Warning $_ }
        }
        throw 'PlateLog did not become ready after service startup.'
    }

    if (-not $SkipFirewall) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $port -LocalAddress '0.0.0.0'
        $listenerProgram = (Get-Process -Id $listener.OwningProcess).Path
        if ($listenerProgram -notin $python,$venvPython) { throw "Unexpected PlateLog listener executable $listenerProgram" }
        New-NetFirewallRule -Name $ruleName -DisplayName 'PlateLog preview (TCP 8765)' -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress $RemoteAddress -Program $listenerProgram `
            -Profile Any -EdgeTraversalPolicy Block | Out-Null
    }
} catch {
    $setupFailure = $_
    try {
        if ($installedService) {
            Stop-Service -Name $serviceName
            & $wrapper uninstall
            if ($LASTEXITCODE -ne 0) { throw 'Could not uninstall the PlateLog service during rollback.' }
        }
        if ($createdApp -and (Get-Service $serviceName -ErrorAction SilentlyContinue)) {
            throw 'A PlateLog service still exists. Inspect it before retrying.'
        }
        $createdDirectories = @(
            if ($createdRoot) { $root }
            if ($createdApp) { $app }
        )
        foreach ($directory in $createdDirectories) {
            $item = Get-Item -LiteralPath $directory
            if ($item.FullName -ne $directory -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Inspect the leftover directory $directory before retrying."
            }
            Remove-Item -LiteralPath $item.FullName -Recurse -Force
        }
    } catch {
        Write-Warning "Rollback stopped. $($_.Exception.Message)"
    }
    throw $setupFailure
} finally {
    $env:PIP_CONFIG_FILE = $priorPipConfig
}

Write-Host "PlateLog register  http://$($env:COMPUTERNAME):$port/"
Write-Host "PlateLog Admin     http://$($env:COMPUTERNAME):$port/admin"
if ($SkipFirewall) { Write-Host 'Have IT allow inbound TCP 8765 for the desks through its firewall policy.' }
else { Write-Host "Inbound rule added for TCP $port from $($RemoteAddress -join ', ')." }
