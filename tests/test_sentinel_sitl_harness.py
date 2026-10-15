"""Protocol regressions in the simulator's pilot, without commanding hardware."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "sitl"))
from gcs_standin import SitlGroundStation


class SigningCleanupTests(unittest.TestCase):
    def test_removal_sends_zero_key_and_timestamp_before_disabling_local_signing(self):
        pilot = object.__new__(SitlGroundStation)
        pilot.target = (1, 1)
        signed = [True]

        def send(system, component, key, timestamp):
            self.assertTrue(signed[0], "the removal must still use the authorized key")
            self.assertEqual((key, timestamp), (bytes(32), 0))

        pilot.link = SimpleNamespace(mav=SimpleNamespace(setup_signing_send=send),
                                     disable_signing=lambda: signed.__setitem__(0, False))
        with patch("gcs_standin.time.sleep"):
            pilot.setup_signing(None)
        self.assertFalse(signed[0])

    def test_installation_keeps_a_current_timestamp(self):
        pilot = object.__new__(SitlGroundStation)
        pilot.target = (1, 1)
        pilot.link = Mock()
        key = bytes(range(32))
        with patch("gcs_standin.time.sleep"), patch("gcs_standin.time.time", return_value=1420070410):
            pilot.setup_signing(key)
        pilot.link.mav.setup_signing_send.assert_called_once_with(1, 1, key, 1000000)
        pilot.link.setup_signing.assert_called_once_with(key, sign_outgoing=True)
