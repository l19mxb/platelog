# PlateLog IT quick start

Tested on **Windows Server 2025** with **CPython 3.15.2 x64** and **SQLite 3.53.4**.

**Before live use:** follow the [hardening guide](docs/HARDENING.md) and record decisions on the [current readiness findings](docs/SOC2-READINESS.md#findings-and-owners). These documents supersede the 17 November assessment included in earlier ZIPs.

## Before you start

- **Files** — Put the approved ZIP and its trusted `.sha256` file in the same server folder.
- **DNS and TLS** — Choose the internal DNS name. Setup offers **IT-managed certificate (preferred)** or **PlateLog-generated certificate (fallback)** with a selectable lifetime; see [certificate options](docs/CERTIFICATES.md).
- **Access** — Allow **TCP 443** from staff networks. Give the runtime account read/execute on the app and venv, read on TLS files, and write on the data folder.

## 1. Install and start on the server

For a fresh install, edit the paths and paste the block into **Administrator PowerShell**. The setup wizard asks for the certificate option and a new Admin passphrase of 14+ characters. Keep the application, data and configuration folders separate.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $Zip = 'C:\Staging\PlateLog-IT-Pilot.zip'
    $Python = 'C:\Path\To\Approved\Python315\python.exe'
    $Package = 'C:\Program Files\PlateLog\pilot'
    $Data = 'C:\ProgramData\PlateLog\pilot-data'
    $SiteName = 'platelog.example.internal'
    $Configuration = 'C:\ProgramData\PlateLog\config'

    $ExpectedHash = ((Get-Content -LiteralPath "$Zip.sha256" -Raw).Trim() -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $ExpectedHash) {
        throw 'ZIP checksum mismatch.'
    }
    if (Test-Path -LiteralPath (Join-Path $Data 'platelog.sqlite3')) {
        throw 'This data folder already contains PlateLog. Use your upgrade procedure.'
    }

    Expand-Archive -LiteralPath $Zip -DestinationPath $Package
    & (Join-Path $Package 'Setup.ps1') -PythonPath $Python -SiteName $SiteName `
        -DataDirectory $Data -ConfigurationDirectory $Configuration
    & (Join-Path $Package 'Start-PlateLog.ps1') -SettingsPath (Join-Path $Configuration 'installation.json')
}
```

After Step 2, stop the foreground process. Use `Start-PlateLog.ps1` with the selected runtime identity in IT's approved supervisor for startup after reboot. Setup does not create a service, startup task or firewall rule.

**Certificate options:** IT-managed is selected by default and uses the expiry set by IT. The generated fallback defaults to **365 days**, selectable from **1–730 days**. Setup displays the exact expiry and public certificate SHA-256. Distribute only its public `.cer` to PCs/cameras; renewal of a self-signed certificate requires distributing the replacement. No automatic renewal is configured.

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

1. Allow **TCP 8766** from the approved camera IP. Use `Start-PlateLog.ps1 -EnableCamera` in the supervisor startup command and restart PlateLog.
2. Give the camera its own credential in **Admin → Axis cameras**. Set its HTTPS POST URL to `https://<name>:8766/api/axis/<id>/events`.
3. Verify camera trust. Send one controlled pass and check the plate, time and photo.

Keep the same `-SettingsPath` when non-default. This enables camera HTTPS on the saved camera port. For direct Python startup, the equivalent options are `--camera-transport https --camera-port 8766`.

## Renew or change the HTTPS certificate

In Administrator PowerShell from the installed package folder:

```powershell
.\Set-PlateLogCertificate.ps1
```

Choose IT-managed or PlateLog-generated and review the expiry. For a generated replacement, prepare its new public trust on clients before the normal supervisor restart. IT can renew managed PEM files at the same paths and restart directly. See [certificate maintenance](docs/CERTIFICATES.md#renewal).

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

## Download diagnostics

**Admin → Diagnostics → choose a period → Download diagnostics ZIP.** Use 24 hours, 2 days, 3 days or all available retained history. Copy the ZIP through the approved transfer process. Nothing is uploaded automatically.

If PlateLog will not start, edit the paths and run this from PowerShell. It uses only Python's standard library and also works with a missing or unreadable database. The output folder must exist; existing files are never overwritten.

```powershell
& {
    $Package = 'C:\Program Files\PlateLog\pilot'
    $Data = 'C:\ProgramData\PlateLog\pilot-data'
    $Output = 'C:\Staging\platelog-support.zip'
    & (Join-Path $Package '.venv\Scripts\python.exe') -I -S -B `
        (Join-Path $Package 'app\platelog\diagnostics.py') --data-dir $Data --days 1 --output $Output
    if ($LASTEXITCODE) { throw 'Diagnostics export failed. Check the paths, permissions and output filename.' }
}
```

Change `--days 1` to `2`, `3` or `all`. Detailed logs keep up to **30 days or 8 MiB** and begin with this version. The ZIP contains runtime versions, safe settings summaries, counts, job outcomes, error types/codes and code locations. Passwords, number plates, photos, raw payloads, addresses and file paths are excluded. `health.json` reports unreadable or limited history. It is a support file, not a database backup.

**CSV imports:** **Replace entire list** is the default. Preview the changes and confirm replacement before applying. Scheduled refreshes also replace the list.

`START-HERE.md` and `HARDENING.md` inside the ZIP cover full deployment, live acceptance and rollback.
