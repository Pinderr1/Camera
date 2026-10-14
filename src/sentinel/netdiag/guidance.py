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
