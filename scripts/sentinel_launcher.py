"""Starts Sentinel without PYTHONPATH set, for Task Scheduler and shortcuts:
    pythonw scripts/sentinel_launcher.py run --config config/site.local.json"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.__main__ import main  # noqa: E402

main()
