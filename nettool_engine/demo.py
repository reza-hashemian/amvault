"""Sample devices, for working on the interface without a vault.

Enabled with NETTOOL_DEMO=1.  Addresses are from the ranges reserved for
documentation, so nothing here can be mistaken for -- or reach -- a real host.

The data is JSON so the browser preview (`npm run dev`) can show the same
devices without an engine behind it.
"""

import json
import os

with open(os.path.join(os.path.dirname(__file__), "demo_devices.json"), encoding="utf-8") as fh:
    DEVICES = json.load(fh)
