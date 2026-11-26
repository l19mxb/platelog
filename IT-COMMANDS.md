# PlateLog IT quick start

Tested on **Windows Server 2025** with **CPython 3.15.2 x64** and **SQLite 3.53.4**.

## Before you start

- **Files** — Put the approved ZIP and its trusted `.sha256` file in the same server folder.
- **DNS and TLS** — Match the internal DNS name to the certificate SAN. Put the leaf and intermediates in `server.pem` and the private key in `server-key.pem`.
- **Access** — Allow **TCP 443** from staff networks. Give the runtime account read/execute on the app and venv, read on TLS files, and write on the data folder.

## 1. Install and start on the server

For a fresh install, edit the settings and paste the block into interactive PowerShell with install access. Set an Admin password of 14+ characters.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $Zip = 'C:\Staging\PlateLog-IT-Pilot.zip'
    $Python = 'C:\Path\To\Approved\Python315\python.exe'
    $Package = 'C:\Program Files\PlateLog\pilot'
    $Data = 'C:\ProgramData\PlateLog\pilot-data'
    $SiteName = 'platelog.example.internal'
    $Tls = 'C:\ProgramData\PlateLog\tls'

    $ExpectedHash = ((Get-Content -LiteralPath "$Zip.sha256" -Raw).Trim() -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $ExpectedHash) {
        throw 'ZIP checksum mismatch.'
    }
    if (Test-Path -LiteralPath (Join-Path $Data 'platelog.sqlite3')) {
        throw 'This data folder already contains PlateLog. Use your upgrade procedure.'
    }

    Expand-Archive -LiteralPath $Zip -DestinationPath $Package
    & $Python -I -B -m venv (Join-Path $Package '.venv')
    if ($LASTEXITCODE) { throw 'Virtual environment setup failed.' }

    $AppPython = Join-Path $Package '.venv\Scripts\python.exe'
    & $AppPython -I -m pip --isolated --disable-pip-version-check --no-input install `
        --no-index --no-cache-dir --only-binary=:all: --require-hashes --no-compile `
        --find-links (Join-Path $Package 'wheels') -r (Join-Path $Package 'requirements.offline.txt')
    if ($LASTEXITCODE) { throw 'Offline wheel install failed.' }
    & $AppPython -I -m pip --isolated --disable-pip-version-check check
    if ($LASTEXITCODE) { throw 'Dependency check failed.' }

    Push-Location -LiteralPath (Join-Path $Package 'app')
    try {
        & $AppPython -E -s -B -m platelog admin-password --data-dir $Data
        if ($LASTEXITCODE) { throw 'Admin password setup failed.' }
        & $AppPython -E -s -B -m platelog pilot --data-dir $Data --allowed-host $SiteName --port 443 `
            --tls-cert "$Tls\server.pem" --tls-key "$Tls\server-key.pem"
    } finally {
        Pop-Location
    }
}
```

After Step 2, stop the foreground process. Use the same `pilot` command and `app` working directory in IT's approved supervisor for startup after reboot.

## 2. Check from a staff PC

Use existing CA trust. If needed, deploy the public root through Group Policy (GPO) and run `gpupdate /target:computer` on the test PC. [Microsoft certificate deployment](https://learn.microsoft.com/en-us/windows-server/identity/ad-cs/distribute-certificates-group-policy)

Use the DNS name from Step 1 in the health check.

```powershell
Invoke-RestMethod -Uri 'https://platelog.example.internal/healthz' -TimeoutSec 10 -ErrorAction Stop
```

Expect **ready** in **pilot** mode. Open `/admin`, select **Access & security**, set the staff password in **Plate log access**, then test staff sign-in in a private browser window.

**Standalone PC.** Run the [trust helper](examples/Trust-PlateLogCertificate.ps1) as the staff user from the handoff folder. Use IT's approved public DER `.cer` and its verified SHA-256.

```powershell
.\examples\Trust-PlateLogCertificate.ps1 -CertificatePath '.\platelog-ca.cer' `
    -ExpectedSha256 '<approved certificate SHA-256>'
```

## Connect one camera

1. Allow **TCP 8766** from the approved camera IP. Add `--camera-transport https --camera-port 8766` and restart PlateLog.
2. Give the camera its own credential in **Admin → Axis cameras**. Set its HTTPS POST URL to `https://<name>:8766/api/axis/<id>/events`.
3. Verify camera trust. Send one controlled pass and check the plate, time and photo.

## Optional email alerts (SMTP)

1. Allow outbound TCP only to the approved SMTP relay and port.
2. In **Admin → Alarm rules → Email delivery (SMTP)**, enter the relay, port, TLS mode and sender address. Prefer **STARTTLS** or **TLS (implicit)**. Use **None** only for an approved internal IP-authorised relay without a password.
3. If authentication is required, supply `PLATELOG_SMTP_PASSWORD_RELAY` through IT's approved service environment and enter that variable name plus the username in Admin. Restart through the normal supervisor after changing the supplied password value.
4. Select **Test connection**, then **Save SMTP settings**. The test sends no email. Add recipients to a rule, start with **Log only**, and verify a controlled **Live** event before handover.

## Change or reset the Admin password

Use **Admin → Access & security → Change administrator password**. For a server reset, use interactive PowerShell and the active data folder. Existing Admin sessions end.

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

`START-HERE.md` and `HARDENING.md` inside the ZIP cover full deployment, live acceptance and rollback.
