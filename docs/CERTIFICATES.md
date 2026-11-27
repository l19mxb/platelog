# HTTPS certificates

The native offline Windows installer offers two choices. **IT-managed** is selected by default.

| Choice | Lifetime | Maintenance |
| --- | --- | --- |
| **IT-managed certificate — preferred** | Defined by the certificate IT supplies. PlateLog does not shorten or extend it. | IT supplies the PEM certificate/intermediate chain and matching private key, distributes issuing-CA trust and maintains renewal. |
| **PlateLog-generated certificate — fallback** | **365 days by default**, selectable from **1–730 days** at setup. Common choices are 90, 180, 365 and 730. | Site administrator distributes the public self-signed certificate, renews it and redistributes the replacement before expiry. |

Setup displays the exact UTC expiry and renewal owner. It validates the private-key match, SAN hostname/IP, certificate chain, Server Authentication purpose and dates using an in-memory TLS handshake. It opens no network connection and changes no Windows trust store. Expired, future-dated, wrong-purpose, wrong-name, untrusted or mismatched material is rejected. A PEM private key must be unencrypted for unattended service startup and protected by filesystem permissions.

Generated certificates use **RSA-3072 / SHA-256**, include the configured DNS name or IPv4 address in SAN and can include additional names using `-AdditionalCertificateName`. They are server certificates, not a local CA. No CA signing key is created. The private key is readable by the runtime account and authorised administrators, while the runtime cannot rewrite its certificate configuration.

Only the public `platelog-server.cer` and its verified SHA-256 are distributed to clients. The existing [workstation trust helper](../examples/Trust-PlateLogCertificate.ps1) supports the self-signed public certificate. Follow the site's managed policy for staff PCs and configure camera trust separately. The private key remains on the server and in approved protected backup storage.

## Renewal

Use the installed package's `Set-PlateLogCertificate.ps1` to renew the generated option or change management mode. Previous certificate material and configuration are retained for rollback; failed validation does not replace the active configuration. Existing application settings, registrations and passages are not reset.

For IT-managed renewal at the same file paths, IT can replace its PEM files and restart the normal supervisor. For a new self-signed certificate, distribute its new public trust before restarting. Remove obsolete trust and expired key versions through the approved process after rollback is no longer needed. Client-side trust does not carry over automatically to a newly generated self-signed certificate.

Renewal and expiry monitoring are **not automatic**. The saved expiry records setup's last validation; inspect the served certificate after an IT-managed file replacement. Schedule renewal and monitoring through the site's existing PKI/operations process. This installer does not add a scheduled task or change firewall rules.

## Setup and security scope

Follow the [native setup guide](../deploy/pilot/README.md) and [IT quick start](../IT-COMMANDS.md). The fallback requires a site policy decision about self-signed trust, lifetime, key backups, renewal ownership and disposal. The [readiness assessment](SOC2-READINESS.md) remains a point-in-time application review; this certificate setup change adds Windows filesystem/key-management and client-trust acceptance checks to each new handover package. Existing test receipts for older ZIPs do not certify a newly rebuilt ZIP.
