# PlateLog SOC 2 readiness assessment

**Reviewed: 26 November 2026 · Australia/Sydney**

**Application source:** `11e7125c5e13564ed96cc218cb38643a951b38eb`

**Scope:** Offline Windows Server pilot, staff HTTPS 443, camera HTTPS 8766, SMTP/VAPIX rules, CSV imports and diagnostic downloads.

## Decision for IT

**Suitable for IT review and isolated pilot testing.** Live use requires IT and the data owner to close or explicitly accept the site gates below. This review found no new critical or high-severity application defect in the paths examined. High-priority deployment and governance gaps remain; the passing tests do not close them.

This assessment supersedes the 17 November readiness review for the application revision above. It is a point-in-time source, dependency and control review, with synthetic tests. It does not establish operating effectiveness at the workplace or provide an independent CPA assurance opinion. The mapping uses the published [AICPA Trust Services Criteria](https://www.aicpa-cima.com/resources/download/2017-trust-services-criteria-with-revised-points-of-focus-2022). A [formal SOC examination](https://www.aicpa-cima.com/resources/landing/system-and-organization-controls-soc-suite-of-services) requires the relevant professional engagement.

Use the [hardening guide](HARDENING.md) for actions and the [IT quick start](../IT-COMMANDS.md) for commands. The review covers the native Python pilot. The legacy HTTP installer, Linux preview and workplace infrastructure are outside this acceptance scope.

## Release and evidence

The exact application ZIP reviewed and tested has SHA-256:

```text
830a5ff4da28b8fb3e3961797a88cc35f88792c59ba8d7d6209c276a6594c383
```

| Evidence | Result and scope |
| --- | --- |
| Offline ZIP | Archive checksum, every manifest member and all 16 wheel hashes verified. Readable Python source; no workplace database, site credentials or private TLS keys packaged. |
| Windows Server 2025, build 26100 | Exact ZIP installed offline using Python **3.15.2**, SQLite **3.53.4**. TLS 1.3 negotiated on staff 443 and camera 8766. Internal Hyper-V switch; no guest default route. |
| Application tests | **72 passed** in the Windows Server acceptance run, and reproduced during this review. |
| Additional security probes | **28 passed**: role separation, Host/Origin and CSRF rejection, form/body limits, invalid diagnostic periods, private-value exclusion and tampered/stale CSV approvals. Disposable data only. |
| Diagnostics | All four periods downloaded over HTTPS in the exact-ZIP VM test. Tests cover missing/corrupt databases, rotation, permissions failures, standalone export and secret/plate/photo exclusion. |
| SMTP and VAPIX | Isolated mocks verified trusted STARTTLS and implicit TLS, authentication, untrusted-certificate rejection, separate action outcomes, duplicate suppression and timeout handling. SMTP connection tests sent no mail. These tests did not verify a workplace mailbox or physical relay. |
| Upgrade/recovery | Installed application bytes matched the tested ZIP. Existing settings, passages and photo hashes were preserved. Synthetic database/image restore, retention and scheduled CSV checks passed. Workplace recovery targets and reboot acceptance remain open. |

The [machine-readable evidence summary](security-evidence-2026-11-26.json) records hashes, dependency versions and probe outcomes. Detailed receipts belong in the private change ticket. Documentation-only revisions do not extend the test scope to changed application files, wheels or runtimes; bind each new application bundle to its own acceptance receipt.

## Current control review

| Area | Controls verified | Residual limit |
| --- | --- | --- |
| Access | Pilot requires a configured Admin passphrase, HTTPS and a specific allowed hostname. Separate staff/Admin sessions, salted scrypt verifiers, eight-hour sessions, Secure/HttpOnly/SameSite cookies and Origin/CSRF checks. | Shared accounts, no MFA or individual download attribution. Staff sign-in and camera authentication can be weakened by configuration. |
| CSV replacement | Replace is the default. Preview, explicit confirmation and a signed receipt bind the file, mode, session and list revision. Changed files, stale approvals and empty imports were rejected. Failed refreshes keep the last good list. | A valid but incomplete scheduled CSV can replace the list. There is no percentage-removal approval threshold. Publisher quality and freshness need monitoring. |
| SMTP rules | Header/address validation, verified TLS, authentication requires TLS, separate secret references, per-action outcomes and no automatic resend after an uncertain result. | Mail contains plates, camera and timestamps. Relay acceptance does not prove inbox delivery. An Admin selects destinations and recipients. |
| VAPIX rules | Timed HTTPS virtual-input URL validation, trusted certificates, separate credentials, correction/settle handling, stale-event holds and deduplication. | An accepted API response does not prove the physical output. Egress and credential-to-device mapping need site enforcement. |
| Diagnostic ZIP | Admin-only POST, Origin/CSRF protection, one collection at a time, allowlisted fields, opaque camera references and no raw database, photos, payloads, addresses or credential values. | Operational metadata remains internal information. ZIPs are not encrypted by the app. Checksums are not signatures or an immutable audit trail. |
| Retention | Default cleanup on; photos 90 days, passages 360 days. Detailed logs up to 30 days or four 2 MiB files; collection input capped at 12 MiB. | Import review retains the latest **30 runs**, not a time window. Mailboxes, exports, source files, USB copies and backups need separate disposal. |

## Findings and owners

**High** below means a site gate before live use, rather than a newly discovered code exploit. Record an owner, due date, evidence and an expiry date for any exception.

| ID / priority | Finding | Closure required |
| --- | --- | --- |
| F1 · High | Shared access cannot identify an individual or provide MFA. An Admin can disable staff sign-in. | **IT / service owner:** require staff sign-in, maintain an access roster and controlled credential custody, and document the policy decision. Implement named authentication if policy requires it. |
| F2 · High | Optional HTTP/no-auth camera intake and Admin-selected outbound destinations expand trust. A secret reference is not bound to an approved destination by the app. | **Network / camera owner:** camera HTTPS plus unique credentials; effective inbound and outbound allowlists; approve Axis/SMTP destinations and matching secrets. A narrow allow rule alone does not override a broad default-allow policy. |
| F3 · High | Photos, raw metadata, skipped CSV values and email alerts create additional copies and retention obligations. | **Data owner / IT:** approve collection, recipients, storage/share permissions, encryption and disposal for every copy, including approved USB and external support sharing. |
| F4 · High | Workplace startup, backups, monitoring and real-device acceptance are unverified. One host remains a single point of failure. | **IT / service / camera owners:** prove reboot startup under the service identity, consistent restore and recovery targets, clock accuracy, real camera delivery and physical output. Keep actions Off or Log only until approved. |
| F5 · Medium | OpenSSL 3.5.9 is in the affected family of the November advisories. Offline patch ownership is required. | **IT platform:** record the applicability decision below, track an approved patched Python runtime and repeat exact-bundle acceptance after a runtime change. Do not substitute an unreviewed DLL. |
| F6 · Medium | Shared access and locally editable, rotating logs cannot provide durable individual audit evidence. A successful health response does not prove end-to-end capture. | **Service / Security:** retain approved off-host change, access and operating evidence; separately monitor camera freshness, jobs, disk space and certificate expiry. |
| F7 · Medium | Valid but incomplete/stale source lists can cause incorrect registration classifications. Import-review retention is count-based. | **List/data owners:** publish complete files atomically, reconcile counts/skips and freshness, keep a known-good file and approve the 30-run history. Suspend dependent Live actions if list quality is uncertain. |
| F8 · Medium | Delivery is best effort. Actions run serially; socket timeouts are per blocking I/O, not an end-to-end delivery deadline. Browser sound also requires an enabled, active workstation tab. | **Service owner:** test representative peak arrivals and unavailable destinations, watch Held/Failed/Partial/Unconfirmed results and verify the fallback procedure. Do not treat notifications as guaranteed delivery. |

**Previous exact-package evidence gap: closed for this lab release.** The current ZIP has a matching Windows Server installation and alarm/SMTP/diagnostics receipt. F4 remains open for the workplace deployment and actual devices.

## Dependency and runtime review

At **17:13 AEDT on 26 November**, the OSV exact-version query returned **no matching advisories for the 16 locked PyPI packages and installer pip 26.2.2**. A known-vulnerable h11 0.14.0 positive control returned advisories. This is a dated feed result, not a guarantee of absence. It does not cover Windows, Axis firmware or all native libraries bundled in wheels. [OSV query API](https://google.github.io/osv.dev/post-v1-querybatch/)

The lab runtime reference reports Python 3.15.2, SQLite 3.53.4 and **OpenSSL 3.5.9**. The Python executable signature was valid for the Python Software Foundation. SQLite 3.53.4 includes the [WAL-reset fix](https://sqlite.org/wal.html#the_wal_reset_bug). Use the [official Python release](https://www.python.org/downloads/release/python-3152/) and the recorded runtime archive hash when approving media.

### OpenSSL applicability — assessed, not patched

The three advisories below describe 12 Low-severity issues affecting the 3.5 family. Their fixes are scheduled upstream for **1 December 2026**, including 3.5.10. That is a future release at this review date; verify availability through the approved distributor before planning an update.

| Advisory | Relevant conditions and assessment for this source |
| --- | --- |
| [10 November](https://openssl-library.org/news/secadv/20261110.txt) — CVE-2026-88822, 88823, 95691, 95692, 107496 | QUIC, direct AES-GCM-SIV operations and CMS encryption are not used by the reviewed application. Ordinary TCP TLS is used for HTTPS and SMTP TLS. |
| [17 November](https://openssl-library.org/news/secadv/20261117.txt) — CVE-2026-35190, 107493, 124709 | The app does not set the Suite B certificate flag, supply a pre-existing PEM-info stack or use QUIC. The advisory distinguishes ordinary TLS CA loading from the vulnerable PEM-stack use. |
| [24 November](https://openssl-library.org/news/secadv/20261124.txt) — CVE-2026-84786, 107494, 121482, 84785 | No DANE or QUIC configuration was found. CMS/PKCS#7 verification is not used; the checked default client context had CRL checking off. |

**Inference:** the documented trigger conditions are not enabled in the reviewed application paths. This is a source/configuration applicability assessment, not proof that the native library has no vulnerability. Reassess if TLS settings, dependencies or runtime change; retain F5 and the platform owner's patch decision.

## Trust Services Criteria mapping

| Criteria | Application evidence | Workplace evidence still required |
| --- | --- | --- |
| CC1–CC5 | Source inventory, this assessment and test records. | Named primary/backup owners, risk acceptance, trial duration and review cadence. |
| CC6 | TLS, role checks, password verifiers, session and CSRF controls. | Approved identities, effective ACLs, credential custody and managed certificate trust. |
| CC7–CC9 | Bounded diagnostics, job/alarm outcomes, hash-locked release and rollback tests. | Monitoring, incident handling, dated patch decisions and change approvals. |
| A1 | Persistent storage, retention and lab restore evidence. | Capacity/peak-load results, reboot test, recovery targets and witnessed workplace restore. |
| PI1 | Camera replay/corrections, signed import previews, bounded exports and action outcomes. | Real-camera reconciliation, source-list quality, report checks and physical/email acceptance. |
| C1 / Privacy | Access controls, redacted support export and retention controls. | Purpose, collection scope, encryption, recipient approval, support-sharing policy and disposal across all copies. |
