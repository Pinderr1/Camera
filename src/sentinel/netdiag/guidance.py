"""Plain-English fix instructions per brand and per operating system.

Menu names vary by model and firmware generation, so instructions name the usual
location and say so. Sentinel only ever *tells* people what to change; it never
changes router, firewall, computer or recorder settings itself."""

from __future__ import annotations

from typing import Any

BRANDS: dict[str, dict[str, Any]] = {
    "hikvision": {
        "label": "Hikvision",
        "template": "hikvision",
        "network_menu": "Menu → Configuration → Network → General (newer firmware: System → Network → TCP/IP)",
        "rtsp": [
            "RTSP is normally on, on port 554. If it was changed, look under Configuration → Network → More Settings "
            "(newer firmware: Network → Advanced → Port).",
            "Channel 1 streams are /Streaming/Channels/101 (main) and 102 (sub); channel 2 is 201/202, and so on.",
        ],
        "notes": [
            "Hikvision's free SADP tool also lists Hikvision devices on your cable network even when their address is wrong.",
            "A new or reset Hikvision device must be activated (an admin password set) on its own screen or with SADP before it can be used.",
            "Several wrong passwords in a row lock the account for about 30 minutes; check the password before retrying.",
            "If a stream connects but shows no picture, set that stream to H.264 and turn off H.264+/H.265+ in the video settings.",
        ],
    },
    "dahua": {
        "label": "Dahua / Amcrest",
        "template": "dahua",
        "network_menu": "Main Menu → Network → TCP/IP (newer firmware: Main Menu → Network → TCP/IP → edit the LAN port)",
        "rtsp": ["RTSP is normally on, on port 554: see Main Menu → Network → Port."],
        "notes": [
            "Dahua's ConfigTool lists Dahua-based devices on your network and shows their addresses.",
            "Dahua recorders lock the account after repeated wrong passwords.",
        ],
    },
    "lorex": {
        "label": "Lorex",
        "template": "dahua",
        "network_menu": "Right-click → Main Menu → Network → General or TCP/IP (names vary by model)",
        "rtsp": [
            "Many Lorex recorders run Dahua firmware: try the Dahua template first.",
            "Some Lorex models limit RTSP or turn it off; check the model's manual if port 554 is closed.",
        ],
        "notes": ["Lorex recorders lock the account after repeated wrong passwords."],
    },
    "reolink": {
        "label": "Reolink",
        "template": "reolink",
        "network_menu": "Menu → Network → Network General (newer firmware: Settings → Network → Network Information)",
        "rtsp": [
            "Recent Reolink firmware ships with RTSP and ONVIF turned off. Turn them on in the Reolink app or client: "
            "Settings → Network → Advanced → Server Settings (or Port Settings).",
        ],
        "notes": ["The Reolink app or Reolink Client lists Reolink devices on your network."],
    },
    "uniview": {
        "label": "Uniview",
        "template": "uniview",
        "network_menu": "Menu → System → Network → Basic (TCP/IP)",
        "rtsp": ["RTSP is normally on, on port 554: see Menu → System → Network → Port."],
        "notes": [],
    },
    "other": {
        "label": "Other or not sure",
        "template": "custom",
        "network_menu": "the recorder's Network or TCP/IP settings on its own monitor",
        "rtsp": ["Look for RTSP or 'Port' in the recorder's network settings and make sure RTSP is enabled (usually port 554)."],
        "notes": ["Most brands have a free 'device finder' tool that lists devices and their addresses."],
    },
}


def brand(name: str | None) -> dict[str, Any]:
    return BRANDS.get(name or "", BRANDS["other"])


def public_brands() -> list[dict[str, str]]:
    return [{"id": key, "label": value["label"], "template": value["template"]} for key, value in BRANDS.items()]


def network_profile_steps(platform: str) -> list[str]:
    if platform == "windows":
        return [
            "Open Settings → Network & internet → Wi-Fi (or Ethernet) and select the network you are connected to.",
            "Under 'Network profile type', choose Private. Do this only for your own home or business network.",
            "Come back here and search again.",
        ]
    return ["Allow incoming UDP replies from the local network in this computer's firewall, then search again."]


def vpn_steps(name: str) -> list[str]:
    return [
        f"Disconnect the VPN '{name}', or turn on its 'Allow LAN access' / 'local network sharing' / split-tunnelling option.",
        "Click Retest.",
        "To watch cameras from outside the property later, use a VPN into this site rather than forwarding ports on the router.",
    ]


def readdress_steps(brand_name: str | None, local: dict[str, str], device_ip: str) -> list[str]:
    """How to move a recorder onto this computer's network. `local` has network, mask, gateway."""
    menu = brand(brand_name)["network_menu"]
    return [
        f"On the monitor connected to the recorder, open the network settings: {menu}.",
        f"Turn on DHCP and save. The recorder will ask your router ({local['gateway']}) for an address in {local['network']}. "
        "Write down the new IP address it shows.",
        f"Or set a fixed address instead: an unused address in {local['network']}, subnet mask {local['mask']}, gateway {local['gateway']}, "
        f"DNS {local['gateway']}. A fixed address is best for a recorder; pick one outside your router's DHCP range if you know it.",
        "Check that the recorder's LAN port (not one of its camera PoE ports) is cabled to the same router or switch as this computer.",
        f"Click Retest. If the recorder now has a new address, Sentinel will look for it; otherwise enter the new address instead of {device_ip}.",
    ]


def temporary_alias_steps(platform: str, device_network: str) -> list[str]:
    if platform != "windows":
        return []
    return [
        f"Advanced alternative, wired Ethernet only: give this computer a second, temporary address in {device_network} "
        "(Control Panel → Network Connections → your Ethernet adapter → Properties → Internet Protocol Version 4 → Advanced → Add). "
        "This needs a fixed address on the adapter. Use it only to reach the recorder's web page and change its network settings, "
        "then remove it. Sentinel never makes this change for you.",
    ]


# New-camera installs: the usual power, network and pairing failures, as
# symptom -> checks. Brand defaults are described as "often" because firmware
# differs; nothing here is changed automatically.
NEW_CAMERA_GUIDE: list[dict[str, Any]] = [
    {"id": "poe", "title": "Wired (PoE) cameras", "problems": [
        {"symptom": "The camera does not power up (no link light, no IR glow at night)", "checks": [
            "Make sure the cable is in a PoE port: many switches power only some of their ports.",
            "Check the switch's PoE budget against the cameras' maximum draw on their datasheets; infrared at night draws more.",
            "Some cameras with heaters or motors need PoE+ (802.3at); check the datasheet.",
            "Passive PoE injectors (often 24 V) are not the same as 802.3af/at PoE: do not mix them.",
            "Try a short cable you know works; a bad crimp is the most common fault on new runs.",
            "Ethernet allows about 100 m per run (90 m of fixed cable). Longer runs need a switch closer to the camera or a PoE extender."]},
        {"symptom": "The camera powers up but cannot be found", "checks": [
            "Cameras that get no address from a router fall back to a factory address on another range (often 192.168.1.64 on Hikvision, 192.168.1.108 on Dahua). Use 'Find & troubleshoot cameras': Hikvision's SADP search finds them even then.",
            "Cameras plugged into a recorder's own PoE ports are usually on the recorder's private internal network. Add them through the recorder's channels instead of directly.",
            "A brand-new Hikvision camera must be activated (admin password set) before it answers logins.",
            "Give each camera a fixed address or a DHCP reservation in the router so it keeps the same address."]},
        {"symptom": "The picture drops out at night", "checks": [
            "Infrared lights raise power draw: check the PoE budget again.",
            "Spider webs and insects near the lens are attracted by infrared and trigger motion; keep the lens clean."]},
    ]},
    {"id": "wifi", "title": "Wi-Fi cameras", "problems": [
        {"symptom": "The camera will not join the Wi-Fi", "checks": [
            "Most Wi-Fi cameras are set up with the maker's app first; Sentinel connects to them locally afterwards.",
            "Many cameras only use 2.4 GHz Wi-Fi. If the router combines 2.4 and 5 GHz under one name, pairing can fail; use a 2.4 GHz network for setup.",
            "Check the Wi-Fi password and that the router does not limit new devices (MAC filtering, device limits)."]},
        {"symptom": "The camera is on Wi-Fi but Sentinel cannot reach it", "checks": [
            "Guest networks and 'AP/client isolation' stop devices reaching each other: put the camera and this computer on the main network.",
            "Turn on RTSP or ONVIF in the camera's app if the brand ships them turned off.",
            "Cloud-only and most battery cameras never offer a local stream; Sentinel cannot use them."]},
        {"symptom": "The video stutters or keeps reconnecting", "checks": [
            "Check the signal at the mounting point; walls and metal siding weaken it a lot.",
            "Move the access point closer, add a mesh node, or run a cable: a wired camera is always more reliable.",
            "Use the camera's sub-stream (640x360-640x480, 5-10 fps) for Sentinel to keep the Wi-Fi load low."]},
    ]},
    {"id": "streams", "title": "Login and video", "problems": [
        {"symptom": "Login fails", "checks": [
            "Check the username and password on the camera's own web page first. Several wrong attempts lock most cameras for a while.",
            "Some brands need a separate account or 'RTSP authentication' setting for streaming."]},
        {"symptom": "Connected, but no picture", "checks": [
            "Set the stream to H.264; turn off H.264+/H.265+ and 'smart codec' options for the stream Sentinel uses.",
            "Use the connection test in Add camera: it reports the codec, resolution and frame rate it actually received."]},
    ]},
]


def new_camera_guide() -> list[dict[str, Any]]:
    return NEW_CAMERA_GUIDE
