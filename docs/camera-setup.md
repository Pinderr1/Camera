# Camera setup and onboarding

Cameras are added in the dashboard under **Setup → Cameras → Add camera**. The wizard runs a real connection test before saving, and every change is validated and applied to the running pipeline immediately. Only the edited camera restarts.

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
