# Camera setup and onboarding

Cameras are added in the dashboard under **Setup → Cameras → Add camera**, or from **Install → Register and connect hardware**, which wraps the same wizard and troubleshooter in the guided installation ([installation.md](./installation.md)). Registering a recorder there groups its channels (cameras linked to it, or streaming from its address). The wizard runs a real connection test before saving, and every change is validated and applied to the running pipeline immediately. Only the edited camera restarts.

## Supported sources

| Wizard choice | Source | Notes |
| --- | --- | --- |
| IP camera (RTSP) | `rtsp` with a brand template | Templates: Hikvision, Dahua/Amcrest/many Lorex, Reolink, Uniview, TP-Link Tapo/VIGI, Axis, or a custom path. These are factory defaults; firmware varies, which is why the wizard tests before saving. |
| DVR / NVR channel | `rtsp` with template and channel | Channel numbering follows the template (e.g. Hikvision channel 3 is `/Streaming/Channels/301` for main and `302` for sub). |
| Find on network (ONVIF) | WS-Discovery, then `rtsp` | Reads device info and media profiles. Picks the largest profile as main and the smallest one at least 320 px wide as sub. Corrects for camera clock skew, which otherwise breaks ONVIF authentication. Only finds devices on the same network segment. |
| HTTP snapshot or MJPEG | `http`, `mode: snapshot` or `stream` | **The fallback for DVRs without RTSP/ONVIF.** Snapshots are polled at the camera fps with Basic or Digest auth. |
| USB webcam or capture device | `usb` / `capture` with an index | Includes analog-to-USB capture cards (the practical route for old analog systems whose DVR exposes nothing usable). Device discovery opens each device briefly, so it runs only on request. |
| Recorded footage | `file` | Looped and paced like a live feed. |
| Simulated camera | `simulated` | Scripted scenario for demos and tests. |

**Do not assume RTSP or ONVIF.** If a recorder supports neither, try:
1. its HTTP snapshot URL (often listed in the manual or the vendor's integration guide);
2. its HDMI/VGA output through a capture card;
3. replacing it with an ONVIF-capable NVR.

## Main and sub streams

Most cameras serve a high-resolution main stream and a low-resolution sub-stream. Sentinel uses the sub-stream for AI processing (`prefer_substream: true`, the default) whenever the camera has one. On the benchmark host, three 1080p30 streams used 55–65% of the CPU just to decode, against 8–21% for 640p sub-streams (ROADMAP findings). The connection test reports resolution, measured fps, codec, and decoder for both streams.

## Hardware video decoding

`hw_decode: "auto"` (default) asks OpenCV/FFmpeg for hardware decoding: D3D11VA on Windows, VAAPI/CUDA on Linux where available. If that fails, it falls back to software. Each camera's status shows whether it engaged ("GPU decode"). On the benchmark host (GTX 1070), D3D11 decoding of a 1080p H.264 file cut CPU from 35% to 12% of the machine. `hw_decode: "off"` forces software decoding, which is useful if a driver misbehaves. H.265 without hardware support is flagged in the test results because it is CPU-expensive.

## Diagnostics

The RTSP test speaks RTSP itself (DESCRIBE with Basic/Digest auth), so it can tell failures apart:

| Result | Meaning / what to do |
| --- | --- |
| `dns_failed` | Host name did not resolve: use the IP address. |
| `unreachable` | Nothing answered: wrong IP, camera off, or a different network/VLAN. |
| `port_closed` | Device answered but the port is closed: enable RTSP, check the port. |
| `auth_required` / `auth_failed` | Credentials missing or wrong. Many DVRs lock accounts after repeated failures. |
| `not_found` | Logged in, but the stream path is wrong: try another template or channel. |
| `not_rtsp` | Another service answered on that port. |
| `no_frames` | Opened but no images: usually a codec the decoder cannot handle (H.265). |
| `timeout` | The test did not finish within 20 s. |

The same classification appears in live camera status (`error_kind`) when a camera drops. Cameras reconnect automatically with backoff (1 s up to 30 s), count their reconnects, and raise a `camera_offline` event after `health.camera_offline_after_s`.

## Network troubleshooter

**Setup → Cameras → Find & troubleshoot cameras** opens a four-step troubleshooter. It also opens from the wizard: from step 1, and from a failed connection test whose result points at the network (`unreachable`, `timeout`, `port_closed`, `dns_failed`, `not_rtsp`), in which case the address and brand are carried over and the diagnosis starts automatically.

1. **Your network.** Shows this computer's adapters (Ethernet, Wi-Fi, VPN, virtual), addresses, router, DHCP, and the Windows network profile. Flags no connection, DHCP failure (169.254.x.x only), no router, several networks at once, a VPN (and whether it carries all traffic), and a Public network profile.
2. **Find devices.** Nothing is searched until the person ticks "I own or manage this network". Then, on this computer's own networks only:
   - **Hikvision SADP** (multicast). Replies are multicast too, so it finds Hikvision recorders **even when their address belongs to another network**.
   - **ONVIF** WS-Discovery, sent out of every local adapter. Replies are unicast, so devices with a wrong address cannot answer.
   - **Optional port check** of 80/554/8000/37777/9000 across at most the /24 around each local address.

   Results say whether each device is on this computer's network or on a different one.
3. **Check a device.** Enter an address, optionally the brand and what the recorder's own screen shows (subnet mask, gateway). Sentinel then:
   - pings once (Windows `IcmpSendEcho`, which tells "host unreachable" apart from "no reply");
   - tries TCP 80, 443, 554, 8000, 9000 and 37777;
   - works out which adapter and router the traffic uses;
   - when nothing answers, checks this computer's router and the recorder's gateway;
   - optionally runs SADP/ONVIF discovery;
   - when the web page answers, makes one unauthenticated HTTP request to recognise the brand.

   Each finding is labelled:
   - **Confirmed problem:** observed directly.
   - **Possible cause:** the evidence points to it but cannot prove it.
   - **Ruled out:** checked, and not the problem. For example, "ping fails but port 80 answers" means the device just ignores ping.
   - **Note** or **OK.**

   Every finding lists its evidence and step-by-step fixes, with Hikvision, Dahua/Amcrest, Lorex, Reolink, Uniview and generic menu paths. **Retest** runs the same checks again, and reports when the result changed or when the recorder now answers at a new address.
4. **Login & streams.** Runs the normal connection test (main and sub-stream, codec, resolution, measured fps, decoder, preview) and can save the camera. **Find recorder channels** sends one RTSP DESCRIBE per channel. It stops at the first login failure, so a wrong password costs one attempt, never one per channel. It lists main/sub status and codec per channel. "Add selected cameras" then adds them in one go, each with its own server-side draft, so the password is sent once.

Boundaries:
- Read-only. Sentinel never changes router, firewall, computer, or device settings; it explains what to change.
- Only private, link-local, carrier-grade-NAT (VPN) and loopback addresses are probed; public addresses are refused (422).
- No MAC addresses, serial numbers, Wi-Fi names, or public IPs are collected or returned.
- Diagnosis, discovery, and channel searches are audited with the address and outcome only.

### Regression case: Hikvision recorder on another address range

The first real install: Windows on Wi-Fi at 10.0.0.57/24 with router 10.0.0.1 and a **Public** network profile. A Hikvision ERI-K108-P8 shows 192.168.1.84 / 255.255.255.0, gateway 192.168.1.254 on its own monitor. Browser and ping fail. The troubleshooter does not assume a single cause:

| What the evidence shows | Diagnosis |
| --- | --- |
| SADP finds the recorder at 192.168.1.84 | **Confirmed**: it is on this cable network with settings for another one (often a previous router or provider). Fix: DHCP or a fixed 10.0.0.x address on the recorder, then Retest |
| Nothing answers, discovery silent, profile Public | **Possible causes**, in order: settings for another network; recorder on a different router/modem; recorder off; Windows blocking discovery replies (switch the profile to Private and search again) |
| The recorder's gateway 192.168.1.254 answers | Different address range **ruled out** (the router reaches that network); the recorder itself is not answering |
| The recorder answers at a new 10.0.0.x address after the fix | Found by discovery and offered as "Check 10.0.0.84"; then reachable, RTSP open, eight channels found |

On the real computer, ping and the browser fail exactly as reported, and SADP and ONVIF found nothing. With the Public profile, that cannot distinguish "recorder not on this network" from "Windows dropped the replies". The troubleshooter therefore reports the mismatch as a possible cause, not a confirmed one.

### Platform support and simulation

- **Windows:** fully supported. Settings come from one read-only PowerShell call (`Get-NetAdapter`, `Get-NetIPInterface`, `Get-NetIPAddress`, `Get-NetRoute`, `Get-NetConnectionProfile`, `Get-NetFirewallProfile`, `Get-DnsClientServerAddress`). The first call can take about 10 s while WMI starts.
- **Linux:** a collector based on `ip -j` exists and is unit-tested, but has not been exercised on a real host.
- **Other systems:** report that inspection is unsupported.
- **Collectors** sit behind `sentinel.netdiag.collect`, and probes behind the `NetworkBackend` interface (`SystemNetwork`).
- **Simulation:** `SimulatedNetwork` answers the same calls from a scenario of adapters, cable segments, NAT routers, and devices with their own IP settings. Automated tests use it. To try the troubleshooter without hardware:

```powershell
python -m sentinel serve --simulate-network config/netsim/hikvision_wrong_subnet.json
```

## Credentials and privacy

- Passwords typed into the wizard go to the credential store (`<storage>/credentials.json`):
  - **Windows:** encrypted with DPAPI, bound to the service account.
  - **Other systems:** an owner-only (0600) file. Use an encrypted volume, or `${ENV_VAR}` placeholders from the service manager.
- The site config stores only a reference (`"credential": "camera.<id>"`).
- A pasted `rtsp://user:pass@host` URL has its password moved to the store automatically.
- API responses and logs never contain stream URLs, stream paths, or passwords:
  - The API returns host, port, brand, channel, and flags only.
  - Library errors are scrubbed.
  - OpenCV/FFmpeg logging is turned down.
- Unsaved connection tests are kept as short-lived server-side drafts (15 minutes), so the browser never receives a stream address.
- Editing a camera without retyping its password or custom path keeps the stored values.

## Zones, masks, map, connections

- **Zones & masks:** draw polygons on a live (privacy-masked) frame. Click points to draw. Drag corners to reshape, drag the small midpoint handles to add corners, right-click a corner to remove it, and drag inside to move.
  - Zone types: `driveway`, `entrance`, `yard`, `sidewalk`, `street`, `perimeter`, `restricted`, `ignore`, `general`. `sidewalk` and `street` activity never counts as on-property.
  - Removing a zone takes it out of rules. A rule left with no zones, or a sequence missing a step, is disabled and reported, never silently widened.
- **Property map:** upload a PNG/JPEG plan, set its size in metres, drag cameras into place, and aim them with the yellow handle. Optionally draw zone footprints. Map coordinates are approximate metres and are kept separate from camera-frame zones; there is no claim of precise spatial calibration.
- **Connections:** which cameras a person can walk between, and in how many seconds. Mark overlapping views. Optional exit and entry zones. Sentinel only links sightings across cameras that are connected here.
- **Categories:** see [environmental-intelligence.md](./environmental-intelligence.md).

The first edit writes a managed copy of the configuration to `<storage>/site.json`, which takes precedence from then on. The original file is never modified, and the last 10 versions are kept in `<storage>/config_history/`.

## What still needs real hardware

None of the following has been exercised against physical devices yet:
- RTSP and ONVIF onboarding on real cameras and DVRs (tested against fake RTSP/ONVIF servers and MediaMTX).
- USB and capture-card discovery.
- HTTP snapshot polling.
- Hardware decoding of live RTSP streams (verified on recorded files only).
- SADP replies from a real Hikvision device (the parser follows the protocol's XML; no device has answered yet), recorder channel search on a real NVR, and the Linux network collector.
