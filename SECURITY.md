# Security policy

## Supported versions

Only the most recent release. Versions are CalVer (`YYYY.M`, e.g. `2026.9.10`)
and roll forward; there are no maintenance branches for older ones.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository. Please don't open a public issue for something exploitable — an
issue here is visible to everyone running this integration before there is a
fix for them to install.

Include the integration version, the module's firmware (`firmType` and the
`mcu`/`wireless` versions from the diagnostics download) and what an attacker
would gain. Expect a first reply within a week; this is a one-maintainer
project.

If a report turns out to be about the module's own firmware rather than this
integration, it belongs with Mitsubishi Heavy Industries — this project can
only document it.

## What this integration touches

- **Local network only.** All device traffic goes to the WF-RAC module's own
  address (default port 51443). The one exception is the firmware update
  check, which is off by default and, when enabled, asks the manufacturer's
  `getFirmware` endpoint for the latest version number for your `firmType`.
- **Credentials.** `operatorId` and `deviceId` are what the module's account
  table recognises; anything holding them can control the unit. They live in
  the config entry, and the diagnostics download redacts them along with the
  host and `airconId`.
- **TLS.** The module presents a self-signed certificate. With a captured
  `ac_cert.pem` in the Home Assistant configuration directory, the connection
  verifies against it (hostname checking stays off — the certificate does not
  carry the unit's address). Without that file, the client falls back to a
  permissive context that does not verify the certificate and allows legacy
  TLS, because the embedded stacks on older modules speak nothing else. That
  fallback is a deliberate trade for reachability on a local network, not an
  oversight. Capture the certificate with:

  ```sh
  openssl s_client -connect <AC_IP_ADDRESS>:51443 -showcerts </dev/null 2>/dev/null \
      | openssl x509 -outform PEM > ac_cert.pem
  ```

- **Writes to the unit.** The integration only ever sends the documented
  `setAirconStat` frames. It does not flash, update or otherwise write to the
  module's firmware.
