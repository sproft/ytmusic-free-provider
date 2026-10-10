"""Release-only installer resolution with stub curl and no Docker/HA host."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReleaseOnlyTests(unittest.TestCase):
    def test_resolution_never_falls_back_to_main(self):
        for name in ("install_provider.sh", "install_watcher_addon.sh"):
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as tmp:
                # Extract the real resolver and its helpers; don't run installation.
                source = (ROOT / "scripts" / name).read_text()
                start = source.index("latest_release_tag() {")
                end = source.index("\n# ", source.index("resolve_ref() {"))
                resolver = source[start:end]
                stub = Path(tmp) / "curl"
                stub.write_text(
                    '#!/bin/sh\nif [ "$RELEASE_FIXTURE" = missing ]; then exit 22; fi\nprintf "%s\\n" "https://github.com/meitetu/ytmusic-free-provider/releases/tag/v1.2.2"\n'
                )
                stub.chmod(0o755)
                program = (
                    'set -eu\nlog(){ :; }\ndie(){ echo "$*" >&2; exit 1; }\nREPO_OWNER=meitetu\nREPO_NAME=ytmusic-free-provider\nREF=""\nRELEASE_ONLY=1\n'
                    + resolver
                    + '\nresolve_ref\nprintf "%s\\n" "$REF"\n'
                )
                environment = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}
                missing = subprocess.run(
                    ["sh", "-c", program],
                    env={**environment, "RELEASE_FIXTURE": "missing"},
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn("refusing to install", missing.stderr)
                self.assertNotIn("main", missing.stdout)
                valid = subprocess.run(
                    ["sh", "-c", program],
                    env={**environment, "RELEASE_FIXTURE": "valid"},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(valid.returncode, 0, valid.stderr)
                self.assertEqual(valid.stdout.strip(), "v1.2.2")
                pinned = subprocess.run(
                    ["sh", "-c", program.replace('REF=""', 'REF="main"')],
                    env=environment,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(pinned.returncode, 0)
                self.assertIn("cannot be combined", pinned.stderr)


if __name__ == "__main__":
    unittest.main()
