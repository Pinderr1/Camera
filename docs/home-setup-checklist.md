# Home Setup Checklist

The goal is to monitor the risky nighttime path while preserving as much privacy as possible.

## Zones

Define these zones before collecting data:

- `bed_zone`: where bed occupancy should be true.
- `bed_exit_zone`: where she first stands or steps out of bed.
- `route_zone`: path between bed and bathroom.
- `bathroom_entry_zone`: doorway or threshold.
- `bathroom_private_zone`: no camera by default.
- `floor_risk_zone`: open floor areas where lying still is concerning.

## Minimum Sensor Layout

- Bed occupancy sensor.
- Manual help/cancel button near bed.
- Route camera or privacy-preserving pose sensor.
- Bathroom door contact sensor.
- Bathroom occupancy sensor if acceptable.
- Router/compute/camera power backup.

Avoid bathroom video. If a bedroom camera is necessary for bedside falls, prefer local pose-only processing, restricted field of view, no audio, and short retention.

## Camera Placement

- Cover the bed exit and walking path, not private areas.
- Avoid mirrors, screens, medication areas, mail, and bathroom interior.
- Test night lighting.
- Confirm the person is visible at normal walking height and floor level.
- Confirm blankets, pets, shadows, and laundry do not look like a person.

## Fall Prevention Fixes

- Clear the bed-to-bathroom path.
- Remove loose rugs, cords, and clutter.
- Add motion night lights.
- Add grab bars and non-slip bathroom surfaces where appropriate.
- Keep footwear stable and easy to put on.
- Keep a phone or help button reachable from bed and bathroom path.
- Consider a bedside commode if the walk itself is the highest risk.

## Reliability Checks

Daily during testing:

- Camera online.
- Bed sensor online.
- Bathroom sensor online.
- Notifications can reach caregivers.
- Local compute has storage.

Weekly:

- Test manual help button.
- Test caregiver acknowledgement.
- Test backup caregiver escalation.
- Review nuisance alerts.
- Review missed or uncertain events.

## Observation Period

Run observation-only for 7 to 14 nights.

Capture:

- Number of bed exits per night.
- Typical bathroom duration.
- Typical time from bed exit to bathroom entry.
- Typical time from bathroom exit to bed return.
- Normal restless sleep movement.
- Normal quiet sleep.
- Sensor dropouts and blind spots.
