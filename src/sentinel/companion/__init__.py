"""Software for the drone's companion computer (Raspberry Pi class).

The companion is a relay, not a brain: it forwards MAVLink between the flight
controller, the Sentinel Hub and the pilot's ground station (filtering what the
Hub may send), relays video, and reports its own health. It runs no detection
and never acts on the aircraft by itself. Standard library only."""
