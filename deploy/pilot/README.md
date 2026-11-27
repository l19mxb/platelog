# PlateLog offline Windows setup

Use IT-approved Windows x64 Python 3.15. The tested reference is **Python 3.15.2 / SQLite 3.53.4**. The ZIP contains readable Python and PowerShell source and local, hash-locked wheels. No internet connection is needed on the server.

## Install

1. Check the ZIP against its separately supplied SHA-256 and extract to a new application folder.
2. Open **Windows PowerShell as Administrator** in that folder.
3. Run setup with the approved Python path:

```powershell
.\Setup.ps1 -PythonPath 'C:\Path\To\Approved\Python315\python.exe'
```

Setup asks for the server DNS name, certificate option and a new Admin passphrase of 14+ characters. It installs only from bundled wheels and configures read-only certificate/application access and writable data access for the selected runtime account. The default runtime identity is **Local Service**. For a domain service account, pass `-RuntimeAccount 'DOMAIN\PlateLogService'` and let IT configure its supervisor.

## Certificate choice

| Option | Setup behaviour | Renewal |
| --- | --- | --- |
| **1. IT-managed certificate — preferred** | Press Enter to select. Supply a server certificate/intermediate chain and matching unencrypted private key in PEM format. The DNS name must be in SAN. Setup validates the key pair, hostname, issuing chain and dates; it leaves IT's files and permissions unchanged. | IT maintains the files and renews according to its PKI policy. Restart PlateLog through the normal supervisor after replacement. |
| **2. PlateLog-generated certificate — fallback** | Creates an RSA-3072/SHA-256 self-signed server certificate. Choose **90, 180, 365, 730 or any whole number from 1–730 days**; default **365**. Setup shows the exact expiry and exports a public `.cer` and SHA-256. | Site administrator creates a replacement, distributes its new public certificate to PCs/cameras, then restarts PlateLog. Renewal is manual. |

IT-managed certificates should already chain to a trusted CA. If setup's account lacks that public trust, pass `-TrustBundlePath 'C:\Staging\approved-ca.pem'` for validation. This does not install a CA or change Windows trust. Staff PCs, cameras and the service's outbound destinations each need their approved trust configuration.

Generated certificates are a fallback for sites without managed PKI. Their **public** `.cer` must be trusted on the clients. Use the [workstation helper](../../examples/Trust-PlateLogCertificate.ps1) with the SHA-256 printed by setup; camera trust is configured separately. Never distribute `server-key.pem`. Setup changes no Windows certificate store or firewall rule.

## Start and hand over

```powershell
.\Start-PlateLog.ps1
```

The default settings file is `C:\ProgramData\PlateLog\config\installation.json`. Use `-SettingsPath` if setup used another configuration directory. Staff/Admin HTTPS defaults to **443** and the listener binds to **0.0.0.0**. Apply IT's inbound rules, internal DNS and client trust before sharing the link. Open `/admin`, sign in and configure staff access.

After configuring a camera, use `Start-PlateLog.ps1 -EnableCamera` to enable dedicated camera HTTPS **8766**. Both listeners use the selected certificate. Staff and camera ports must differ.

IT should register its approved supervisor/startup task with the selected runtime identity and the package as working directory. Setup does not install a service wrapper, register a startup task or start the application automatically. Verify reboot startup and file/share access under the actual identity.

## Renew or change certificate

From the installed package folder, in Administrator PowerShell:

```powershell
.\Set-PlateLogCertificate.ps1
```

This opens the two certificate choices again and shows the validated expiry. Generated renewals preserve the existing SAN names unless `-AdditionalCertificateName` is supplied. The previous material is retained for rollback. For a generated renewal without prompts:

```powershell
.\Set-PlateLogCertificate.ps1 -CertificateMode PlateLogGenerated -CertificateDays 365 -NonInteractive
```

Prepare trust for the new public certificate on staff PCs and cameras before the normal supervisor restart. Remove superseded client trust and old key copies through the approved PKI/retention process after the rollback window. IT-managed renewal can replace its PEM files at the same paths and restart; rerun this helper if paths or management mode change. A failed validation leaves the active configuration intact.

The setup certificate expiry is recorded in `certificate.json` with its validation time; a live TLS connection is authoritative after IT replaces files. There is no automatic renewal or expiry-notification service.

See [IT commands](../../IT-COMMANDS.md), [certificate details](../../docs/CERTIFICATES.md) and the [hardening guide](../../docs/HARDENING.md) for acceptance and site controls.

## Build a handover ZIP

On a connected staging workstation, check out the reviewed Git revision and obtain the exact approved wheels listed in `windows-x64-cp315.tsv`. The server still installs offline.

```powershell
python deploy/pilot/build_pilot.py --wheels C:\Staging\wheels --output C:\Staging\PlateLog-IT-Pilot.zip
```

The builder freezes a committed revision, validates wheel hashes, includes setup/test source and current public security documents, and writes a package manifest, revision record and companion checksum. Identical inputs produce identical ZIP bytes. Existing release archives are never overwritten. Retain the matching Windows Server acceptance receipt for the delivered ZIP.
