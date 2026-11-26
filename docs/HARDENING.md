# PlateLog Windows pilot hardening guide

**26 November 2026** · Use with the [readiness assessment](SOC2-READINESS.md) and [IT quick start](../IT-COMMANDS.md).

## 1. Approve the release

- Record the application source, ZIP SHA-256, Python signature/runtime hash, wheel inventory and matching Windows Server acceptance receipt in the change ticket. The reviewed application is `11e7125`; the assessment gives its full hash and ZIP checksum.
- Install the readable-source package using the approved **offline Python** procedure. The tested reference is **Python 3.15.2 x64 / SQLite 3.53.4**. Use the native `pilot` entry point; the legacy HTTP preview installer is outside this deployment scope.
- Assign the service owner, backup owner, Windows platform owner, camera owner and data owner. Agree the trial period, outage procedure, recovery time and acceptable data loss.
- Record a current vulnerability decision. OpenSSL 3.5.9 needs the documented November applicability review and a patch owner. Upstream fixes are scheduled for 1 December; approve an available distributor runtime and repeat acceptance before replacing it. OSV's PyPI check does not clear native libraries, Windows or firmware.

## 2. Restrict network access and establish trust

| Flow | Required configuration | Pass condition |
| --- | --- | --- |
| Staff PCs → PlateLog | Internal DNS name matching the certificate SAN; trusted HTTPS **443**; allow only approved staff networks. | A managed PC opens the DNS URL without a warning. An agreed out-of-scope source is denied. |
| Axis cameras → PlateLog | Dedicated HTTPS **8766** or approved alternate; unique HTTP Basic credentials for each camera ID; restrict source IPs. Leave intake off until setup is ready. | Camera trusts the server and sends one approved crossing with the correct plate, photo and time. Unauthenticated intake is denied. |
| PlateLog → VAPIX device | Effective egress allowlist to each approved HTTPS destination and port; least-privileged device credentials. | Approved destination works; an agreed unapproved destination is blocked. A narrow allow rule added to a default-allow policy is insufficient. |
| PlateLog → SMTP relay | Effective egress allowlist to the approved relay and configured port. Prefer STARTTLS or implicit TLS. | Trusted TLS and required authentication work under the service identity. No TLS downgrade is accepted. |
| Management, DNS, time, SMB, monitoring, backup | Allow only the site's required internal services and approved management sources. Keep public internet access blocked. | Effective IPv4/IPv6 rules and the required internal paths are recorded. |

Set `--allowed-host` to the canonical DNS name. Use HTTPS bookmarks and block unintended staff HTTP listeners. Issue a Server Authentication certificate with the DNS name in its SAN and serve the intermediate chain. Distribute public CA trust using the site's managed policy; camera trust is separate. Assign a renewal owner and verify both clients after renewal. Use the [workstation trust steps](../IT-COMMANDS.md#2-check-from-a-staff-pc).

## 3. Apply service-account and access controls

- Run one process per data directory under IT's approved supervisor and a dedicated non-admin identity. The app and venv should be read/execute; the runtime must not be able to replace its own code.
- Restrict the TLS private key to the runtime and authorised administrators. Grant Modify only on the local data/log folders and approved report output; grant read on the approved CSV source. Protect data, reports and backups with site-approved encryption and ACLs.
- Use explicit UNC paths for shares. **Local Service presents anonymous credentials remotely**; authenticated shares need an approved identity and suitable share/NTFS permissions. Test actual file access under that identity, not just an Administrator session. [Microsoft LocalService account](https://learn.microsoft.com/en-us/windows/win32/services/localservice-account)
- Set a unique Admin passphrase before pilot launch. In **Admin → Access & security**, configure separate staff credentials and keep staff sign-in required. Verify anonymous access is denied and staff cannot use Admin or diagnostic downloads.
- The app uses shared credentials. Record the access roster, custody, rotation and departure procedure. If policy requires named identities, MFA or individual export attribution, implement and verify that requirement before live use.

## 4. Commission lists, retention and reports

1. **Approve collection.** Record camera coverage, stored metadata/photos and the business purpose. Original photos can contain more than the plate crop.
2. **Check retention.** Keep automatic cleanup enabled. Fresh defaults: photos **90 days**, passages **360 days**. Import review retains the latest **30 runs**, not 30 days. Approve separate retention for registered/source lists, mailboxes, reports, backups and support copies.
3. **Validate the CSV publisher.** Use a read-only source for PlateLog; grant publishing access only to its owner. Publish the complete file atomically. Confirm expected plate counts, authorisation fields, skips and freshness before enabling scheduled replacement. A valid but incomplete file can replace the list without a size-change approval threshold.
4. **Test replacement safely.** **Registered vehicles → Import mode** defaults to Replace. Review additions/removals and explicitly confirm. Keep a known-good file for recovery. Prove an invalid/empty file leaves the current list intact. Investigate skipped entries; they can contain original source values.
5. **Test reports.** In **Daily reports**, run **Test folder** using the actual runtime context. Verify a prior-local-day CSV with headers and expected counts. Existing date files are preserved; agree how to check/backfill missing reports after downtime. Test a bounded Excel export and approved photo use.
6. **Set time and capacity.** Use the site's internal time source for Windows and cameras. Display time zone does not fix device clock drift. Size disk and backups using measured photos and peak traffic; monitor free space and capture/receipt discrepancies.

## 5. Commission notifications

Keep rules **Off** or **Log only** until their destination is approved. No real relay or workplace email delivery was exercised by the lab review.

### VAPIX

Record the source camera, trigger, destination, timed virtual input, duration, expected physical effect and credential mapping. Supply each password through the approved supervisor/vault environment using its `PLATELOG_AXIS_RELAY_PASSWORD_…` reference. Verify certificate trust and effective egress under the runtime identity. In an approved test window, witness a controlled output and its return to idle. **Accepted** confirms the API response, not the physical effect.

### Email (SMTP)

Use **Admin → Alarm rules → Email delivery (SMTP)**. Approve the relay, sender and every recipient. Mail includes the plate, camera and timestamps, including the plate in the default subject; mailbox access and retention form part of the data boundary. TLS protects transport, not all stored copies.

For password authentication, use the `PLATELOG_SMTP_PASSWORD_…` reference and TLS. **Test connection** checks connectivity/TLS/authentication without sending email. Separately verify one controlled message reaches the approved mailbox. **Accepted** means relay acceptance, not inbox delivery. Use unencrypted, unauthenticated relay mode only if specifically approved and isolated by IT.

### Operator acceptance

Test registered/unregistered matches, wrong camera, duplicates, corrected and stale reads, unavailable destinations and Off. Confirm recipients and physical results independently. Actions are processed serially with socket timeouts; test representative peak arrivals and failure conditions. A timeout may follow successful delivery, so **Unconfirmed** is not an instruction to resend. Investigate **Held**, **Failed** and **Partial** results. Browser sound needs an operator gesture, a suitable volume and an active page; verify it at each operator desk. Keep the approved manual procedure available.

## 6. Handle diagnostic files and operating evidence

Use **Admin → Diagnostics** for 1, 2, 3 days or all retained history. The [command-line exporter](../IT-COMMANDS.md#download-diagnostics) works when the web app cannot start.

Detailed logs retain up to **30 days or 8 MiB**, whichever is reached first, and begin with the diagnostics release. Collection limits each activity file to 5,000 entries and bundle input to 12 MiB. Check `health.json` for missing, unreadable or limited history. Collection contacts no camera, relay, mail server or upload service.

The ZIP excludes raw payloads, plates, photos, addresses, credential values and file paths. It still includes internal operational metadata. The app does not encrypt the ZIP. Use approved encrypted media/transfer, inspect it before sharing and follow the site's external-support/AI policy. Record the fault time and observed behaviour, then apply the approved disposal period to the copied file.

Diagnostic logs are best effort, locally editable and rotating. ZIP checksums do not establish authenticity. Keep approved change/access/incident evidence off the application host. Monitor HTTPS readiness, expected camera reads, list freshness, scheduled jobs, disk space, certificate expiry and alarm outcomes separately. A healthy web page alone does not demonstrate capture or delivery.

## 7. Prove recovery and sign off

- In an agreed window, stop capture and the application cleanly. Back up the complete data directory and referenced photos to approved protected storage, along with the release, launch settings and certificate/secret recovery references. An online SQLite backup alone does not pin photos against cleanup.
- Restore to an isolated location with intake, scheduled shares and Live actions disabled. Check database integrity/foreign keys, record and photo counts/hashes, sign-in and retained settings. Measure recovery time and recovered data age.
- Retain the previous approved package and matching backup for rollback. Preserve current evidence before reverting and reconcile passages and report dates since the backup.
- Prove startup after a reboot using the actual supervisor and identity. Record Network/PKI, data-owner, camera-owner and operations approvals. Assign incident escalation and an end-of-trial disposal/ownership decision.

Live acceptance is complete only when the change ticket contains the exact release identity, effective network/ACL evidence, real-camera and notification results, restore/reboot evidence and the owners' signed decisions on open findings.
