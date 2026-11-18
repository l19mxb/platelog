# PlateLog IT commands

The offline Windows Server pilot was verified on Windows Server 2025 with **CPython 3.15.2 x64**, **SQLite 3.53.4**, and staff HTTPS on **TCP 443**. The ZIP's `START-HERE.md` and `HARDENING.md` cover the full deployment.

## 1. Install and start on the server

Stage the approved offline ZIP and its SHA-256. Set up the internal DNS name and an HTTPS certificate that names it. Place the server certificate first in `server.pem`, followed by any intermediates. Put the matching key in `server-key.pem`. Give the runtime account read access to the TLS files, read and execute access to the app, and write access to the data directory. Scope TCP 443 to approved staff networks.

Replace the example values and select a fresh extraction folder. Run in interactive PowerShell with permission to install there. The Admin password prompt requires at least 14 characters.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $Zip = 'C:\Staging\PlateLog-IT-Pilot.zip'
    $ZipSha256 = '<approved ZIP SHA-256>'
    $Python = 'C:\Path\To\Approved\Python315\python.exe'
    $Package = 'C:\Program Files\PlateLog\pilot'
    $Data = 'C:\ProgramData\PlateLog\pilot-data'
    $SiteName = 'platelog.example.internal'
    $Tls = 'C:\ProgramData\PlateLog\tls'

    if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $ZipSha256) {
        throw 'PlateLog ZIP SHA-256 mismatch.'
    }
    if (Test-Path -LiteralPath (Join-Path $Data 'platelog.sqlite3')) {
        throw 'This data directory already has a PlateLog database. Follow your approved upgrade procedure.'
    }

    Expand-Archive -LiteralPath $Zip -DestinationPath $Package
    & $Python -I -B -m venv (Join-Path $Package '.venv')
    if ($LASTEXITCODE) { throw 'Could not create the virtual environment.' }

    $AppPython = Join-Path $Package '.venv\Scripts\python.exe'
    & $AppPython -I -m pip --isolated --disable-pip-version-check --no-input install `
        --no-index --no-cache-dir --only-binary=:all: --require-hashes --no-compile `
        --find-links (Join-Path $Package 'wheels') -r (Join-Path $Package 'requirements.offline.txt')
    if ($LASTEXITCODE) { throw 'Offline wheel install failed.' }
    & $AppPython -I -m pip --isolated --disable-pip-version-check check
    if ($LASTEXITCODE) { throw 'Offline dependency check failed.' }

    Push-Location -LiteralPath (Join-Path $Package 'app')
    try {
        & $AppPython -E -s -B -m platelog admin-password --data-dir $Data
        if ($LASTEXITCODE) { throw 'Admin password setup failed.' }
        & $AppPython -E -s -B -m platelog pilot --data-dir $Data --allowed-host $SiteName --port 443 `
            --tls-cert (Join-Path $Tls 'server.pem') --tls-key (Join-Path $Tls 'server-key.pem')
    } finally {
        Pop-Location
    }
}
```

Open `/admin` over HTTPS and set the separate staff password in **Register access**. After the initial check, stop the foreground process and run the same `pilot` command from the `app` directory under IT's approved supervisor. Camera intake is enabled separately. [Camera setup](README.md#offline-windows-server-pilot)

## 2. Trust the certificate on staff PCs

If the issuing CA is already trusted, use the existing trust. Otherwise, IT can deploy the public root CA certificate to the approved managed PCs through Group Policy under **Computer Configuration → Policies → Windows Settings → Security Settings → Public Key Policies → Trusted Root Certification Authorities**. On a test workstation, refresh computer policy with `gpupdate /target:computer` and follow the site's restart policy. Keep the server private key on the server or in approved key storage. [Microsoft certificate deployment](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/distribute-certificates-group-policy) · [gpupdate](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/gpupdate)

For an approved manual import, place the [certificate trust script](examples/Trust-PlateLogCertificate.ps1) and the approved public root CA or self-signed server certificate in the same folder. Use DER `.cer` format. Run as the staff user with the certificate's verified SHA-256. The script imports into that user's trust store.

```powershell
Set-Location -LiteralPath 'C:\IT\PlateLog'
.\Trust-PlateLogCertificate.ps1 -CertificatePath '.\platelog-ca.cer' `
    -ExpectedSha256 '<approved certificate SHA-256>'
```

From a staff PC, run the HTTPS check and then open the same hostname in the browser. The health check should report `status: ready` and `mode: pilot`.

```powershell
Invoke-RestMethod -Uri 'https://platelog.example.internal/healthz' -TimeoutSec 10 -ErrorAction Stop
```

## 3. Update or reset the Admin password

While signed in, use **Admin → Change administrator password**. To reset it from the server, use interactive PowerShell with write access to the active data directory. Use the same `--data-dir` value as the running pilot. A successful change ends existing Admin sessions.

```powershell
& {
    $Package = 'C:\Program Files\PlateLog\pilot'
    $Data = 'C:\ProgramData\PlateLog\pilot-data'
    if (!(Test-Path -LiteralPath (Join-Path $Data 'platelog.sqlite3') -PathType Leaf)) {
        throw 'Set $Data to the active PlateLog data directory.'
    }
    Set-Location -LiteralPath (Join-Path $Package 'app') -ErrorAction Stop
    & (Join-Path $Package '.venv\Scripts\python.exe') -E -s -B -m platelog admin-password --data-dir $Data
}
```
