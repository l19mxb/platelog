# PlateLog technical design notes

PlateLog is a local register of vehicle passages reported by Axis License Plate Verifier cameras. It uses one FastAPI process, server-rendered HTML, SQLite in WAL mode, and JPEG files on persistent storage.

The [offline Windows Server pilot](README.md#offline-windows-server-pilot) serves staff HTTPS on TCP `443` by default and can enable HTTPS camera intake on TCP `8766`. Staff and Admin use separate shared credentials. The pilot includes Excel exports, nightly CSV, a 24-hour activity chart, and timed HTTPS VAPIX rules. The [IT command sheet](IT-COMMANDS.md) covers deployment.

The design sections below retain proposals for later work, including individual accounts, per-user export auditing, a selected-day timeline and evidence ZIP archives.

## Camera intake

Start with one camera and the native Axis HTTP POST integration. Axis documents JSON with embedded base64 images and multipart with separate image parts. Use JSON with images enabled for the first trial, then use the capture to confirm the exact field names and payload shape before claiming support for that camera and app version. Multipart is the second format to verify. Enable the Axis event buffer where supported and test what it replays after a receiver outage. The built-in **Test** button can verify delivery with a virtual plate; a controlled pass by a test vehicle verifies the real photo and direction. [Axis user manual](https://help.axis.com/en-us/axis-license-plate-verifier) · [Axis push event API](https://developer.axis.com/vapix/applications/license-plate-verifier-api/)

- Give each registered camera its own receiver path and credential. Use the authentication mode verified on that camera. Use HTTPS with a certificate the camera trusts. Keep the receiver on the approved private network and restrict it to camera sources. Camera credentials grant intake only. Use the authenticated camera registration for identity, and check a reported serial number against the registration when one is configured.
- Read `capture_timestamp` as UTC epoch milliseconds and save server receipt time separately. Preserve the reported plate text and its corrections; store a normalised search key separately. Capture `carState`, `carMoveDirection`, `carID`, `packetCounter`, profile and area of interest when present. Keep the remaining JSON metadata without embedded image bytes. Use the configured site timezone for display and day boundaries.
- Set explicit limits on the body, image count and decoded image size. Validate JPEGs before storing them. Never fetch `imagesURI` or use camera paths as local filenames. Missing photo bytes are valid and visible as “No image received”; an invalid attachment is a reject logged without the plate text, raw body or credentials. Return success only after durable storage. An exact replay also returns success.

## One passage, multiple packets

Axis sends `new` at first recognition, `update` when a reading changes and `lost` when tracking ends. `new` has no direction. A later packet can correct the number or supply a direction. `carID` is a runtime identifier, so it cannot identify a passage forever. [Axis push event API](https://developer.axis.com/vapix/applications/license-plate-verifier-api/)

1. Give each accepted packet an internal ID. Use a unique fingerprint scoped to the registered camera, derived from canonical event JSON without inline images and the ordered image hashes. Keep the packet history. A changed plate, direction or image is new information, even if it uses a previously seen counter.
2. Give each passage a separate stable ID. Correlate packets only within the same camera, profile and area, using `carID` within a bounded capture-time window. Set the window after checking the test camera’s timings. Treat `lost` as closure. A fresh `new` after closure can start another passage; a delayed packet may join a closed passage only when its capture time fits that passage unambiguously. An ambiguous packet stays separate and is flagged for review. Never merge by plate text alone.
3. Show one row per passage. Order by first capture time, display the newest usable plate and known direction, and keep earlier values in packet history. A delayed packet or an unknown direction must not replace newer known information. A later packet with no image must not remove an earlier photo. Show the photo’s source packet and capture time if it came from an earlier reading.

## Proposed register and access model

Open on the latest passages, with camera, local capture time, direction and photo availability in each row. Offer search by plate, camera, date and time range, and direction. A selected-day timeline scrubs to detections using the same filters. On days when the clocks change, use the actual local-day boundaries; disambiguate repeated times with the UTC offset. Refresh the latest view with a short poll for new or revised passages, and leave the operator’s place alone while they inspect an older page or open a photo.

| Role | Permissions |
| --- | --- |
| Operator | View and search the register, photos, timeline and operational system log. |
| Supervisor | Operator access plus XLSX and evidence ZIP exports. |
| Administrator | Supervisor access plus users, cameras, retention, archives and audit log. |

Use individual local accounts with Argon2id password hashes. Bootstrap the first administrator out of band with no default password. Use expiring server-side sessions and Secure, HttpOnly cookies over HTTPS, rate-limit login attempts, and protect browser write actions against CSRF. Check role permissions on every server route, including photo and download routes. Keep photos outside the public web root. Revoke sessions when an account is disabled or its password or role changes.

## Proposed data and HTTP boundaries

Use indexed SQLite tables for cameras, accounts and sessions, passages, packets, images, settings, system events, audit and archive runs. Index passage time, camera/time, and normalised plate. Use a stable `(capture_time, passage_id)` cursor for register pages. Keep database and JPEG files on local persistent storage. Generate the photo path server-side and store its packet ID, byte count and SHA-256. Place the verified file durably before committing the SQLite reference, acknowledge after commit, and reconcile unreferenced files left by an interrupted write. A restart must not detach a photo from its event.

| Proposed route | Purpose |
| --- | --- |
| `POST /api/axis/{camera_id}/events` | Authenticate and ingest one Axis payload. |
| `GET /api/passages` | Filtered and paginated register; supports polling for changes. |
| `GET /api/passages/{id}` | Passage with its packet history and photo references. |
| `GET /api/images/{id}` | Serve an authorised JPEG by ID. |
| `POST /api/exports` | Apply the current filters and create an audited XLSX or evidence ZIP. |
| `GET /healthz` | Report basic service readiness without exposing plates, cameras or settings. |

XLSX exports use the same filters as the register and write plate values as text. Evidence ZIPs include the records, JPEGs, and a manifest linking passage IDs, packet IDs, filenames and SHA-256 digests. Record the requesting account, time, filters, result count and outcome. Keep the audit log separate from the system log, which reports camera intake failures, storage issues and maintenance results without raw plates or credentials.

## Retention, recovery and proposed archives

Allow administrators to set separate image and record retention. If an image expires first, leave an explicit “Image expired” status on the record. Deleting an expired record removes its linked live images. Manual and scheduled archives preserve IDs, original packet metadata, photos and a checksum manifest; verify an archive before treating it as eligible to satisfy any archive-before-delete setting. Configure and confirm archive retention separately so a live purge does not imply deletion from archives or backups.

Use SQLite’s backup API for a consistent database copy. Pin the images referenced by that snapshot against retention until they have been copied and verified. Keep backups on approved separate storage and perform a restore test that checks records and JPEG hashes. Log and alert on failed maintenance or low disk space; never report a successful ingest after a write fails.

## Build and pilot gates

1. **Ingest and replay.** Verify a synthetic JSON payload with a JPEG and a metadata-only payload. Confirm that replay keeps one packet, two cameras can use the same runtime ID, a correction keeps the original evidence, and delayed data cannot rewrite newer state. Verify disk-write failure and recovery.
2. **Register and accounts.** Verify search, filters, the 24-hour activity chart, auto-refresh and photo access. Confirm the register and downloads require staff or Admin sign-in when staff sign-in is enabled. Verify the Admin routes require the Admin credential.
3. **Export and housekeeping.** Verify filtered Excel exports with and without photos and a scheduled daily CSV. Exercise retention and restore both the database and photos in a disposable instance.
4. **Single-camera pilot.** Record the Axis model, License Plate Verifier version, enabled push options and a sanitised sample request. Verify the built-in Test first, then a controlled real pass with image, direction, timestamps and replay. Expand to other cameras only after the first one passes.

For the Windows Server target, use the [offline HTTPS pilot](README.md#offline-windows-server-pilot) with the approved native Python runtime and a local persistent data directory. Staff HTTPS defaults to TCP 443. The [legacy WinSW installer](deploy/windows/README.md) runs a synthetic HTTP preview. The Docker image requires a Linux engine and remains suitable for a Linux VM. Verify reachability from the first camera and authorised browser clients, and verify startup after a reboot, before accepting the host.
