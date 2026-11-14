# PlateLog offline Windows Server preview

The x64 ZIP includes a signed Python 3.13.17 offline installer, WinSW 2.12.0 for .NET Framework, the application and the full wheelhouse. PlateLog stores its SQLite database and synthetic JPEGs on the server. It needs no Internet access, Docker, database service or Axis camera. The server needs Windows PowerShell 5.1 or newer and .NET Framework 4.x. [Python release](https://www.python.org/downloads/release/python-31317/) · [WinSW release](https://github.com/winsw/winsw/releases/tag/v2.12.0) · [.NET Framework on Windows Server](https://learn.microsoft.com/en-us/dotnet/framework/get-started/system-requirements)

## Install over RDP

1. Compare the ZIP's SHA-256 against the separate `.sha256` file and scan the bundle. `COMPONENTS.tsv` lists the versions, sources and checksums. Python is signed by the Python Software Foundation. WinSW and `Setup.ps1` are unsigned. Use IT's application-control and script-signing process if required.
2. Transfer the ZIP using the site's approved process. If RDP drive redirection is allowed, select the transfer drive under **Local Resources → More → Drives** before connecting. Copy the ZIP to an administrator-controlled location on the server and extract it to a fresh local folder. [Microsoft drive redirection policy](https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-remotedesktopservices#donotallowdriveredirection)
3. Open **PowerShell as Administrator** in the extracted folder and run the setup. Enter and confirm a new PlateLog admin password of at least 14 characters at the prompt. If the server requires signed scripts, have IT sign the reviewed script or apply its standard script approval process.

   ```powershell
   .\Setup.ps1
   ```

   Setup checks the bundle, installs Python if required, installs the locked dependencies offline, registers and starts the `PlateLog` service, and checks `/healthz`. The service starts at boot under `LocalService`. Code lives under `%ProgramFiles%\PlateLog`. Data and logs live under `%ProgramData%\PlateLog`. Setup is for a fresh install and stops if it finds an existing one.

4. From another desk, open `http://SERVER-NAME:8765/` using the server's hostname or IPv4 address. Check the two sample rows and photos, search for `AO1`, and open the Excel report. Open **Admin** at the same address and use the password set during setup to change retention settings.

## Network access

The preview listens on all IPv4 interfaces (`0.0.0.0:8765`). Setup adds a Windows Firewall allow rule for the PlateLog listener on TCP 8765 from `Any` source by default. Routing, network firewalls and Group Policy still determine which desks can connect. The register, photos and Excel export are available to anyone who can reach the listener. This bundle contains only synthetic plates. Admin requires the password set during setup. HTTP carries the password and session cookie in clear text. [Windows Firewall rule address and port scoping](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)

To limit the rule to selected desks or subnets, substitute the real addresses in this example:

```powershell
.\Setup.ps1 -RemoteAddress '10.20.30.18','10.20.40.0/24'
```

Use `-SkipFirewall` if IT manages the allow rule centrally. Use `-VerifyOnly` to check the extracted files without installing anything. If IT already supplies machine-wide x64 Python 3.13 with `venv` and `ensurepip` at another location, use `-PythonPath 'C:\path\to\python.exe'`.

Keep RDP access under the existing IT policy. The preview needs no outbound Internet access or camera network rule.

## Backup and removal

Stop the service, copy the entire data directory to approved storage, then start the service again. To remove the preview, run the following from an administrator PowerShell. Keep or dispose of the data under the site's retention policy.

```powershell
Stop-Service PlateLog
& "$env:ProgramFiles\PlateLog\PlateLog.exe" uninstall
if ($LASTEXITCODE -ne 0) { throw 'Could not uninstall the PlateLog service.' }
Get-NetFirewallRule -Name 'PlateLog-Preview-TCP-8765' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
```

## Before a workplace camera pilot

Wire authenticated Axis pushes into the persistent register on a dedicated ingest listener. Permit only the approved camera IPs to reach that listener. Gate the register, images and export routes for authorised operators before admitting real captures. Verify the actual Axis firmware with a controlled vehicle pass, then test service restart and a restore on the target Windows Server.

## Building the offline bundle

From the repository's `platelog` directory on a connected Windows x64 staging machine with Python 3.13, use `python -m pip download --only-binary=:all: -r requirements.txt -d wheels` to populate a fresh wheel directory. Review the resolved versions, fetch the pinned vendor files from their release pages, then run `python deploy/windows/build_offline.py --wheels wheels --vendor vendor --output PlateLog-Windows-x64-Offline.zip`. The builder emits the ZIP, its SHA-256 sidecar and an internal hashed dependency lock. [pip download](https://pip.pypa.io/en/stable/cli/pip_download/)
