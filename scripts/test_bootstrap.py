"""Small offline macOS installer fixtures; no host tools or Docker are changed."""

import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(platform.system() == "Darwin", "macOS shell bootstrap fixture")
class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "bootstrap.sh", self.root / "bootstrap.sh")
        self.stub = self.root / "stubs"
        self.stub.mkdir()
        archive = self.root / "fixture.tar.gz"
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        self.target = self.root / ".cache" / "host-tools" / f"node-24.21.0-darwin-{arch}"
        with tarfile.open(archive, "w:gz") as tar:
            for name, output in (("node", "v24.21.0"), ("npm", "11.19.0")):
                data = f"#!/bin/sh\nprintf '%s\\n' '{output}'\n".encode()
                info = tarfile.TarInfo(f"node-v24.21.0-darwin-{arch}/bin/{name}")
                info.mode = 0o755
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.root / "scripts" / "host-tools.manifest").write_text(
            f"platform|tool|version|sha256|url\n"
            f"darwin-{arch}|node|24.21.0|{digest}|https://example.invalid/fixture\n"
        )
        stub = self.stub / "curl"
        stub.write_text("#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do\n"
                        "  if [ \"$1\" = -o ]; then cp \"$B71_FIXTURE_ARCHIVE\" \"$2\"; exit $?; fi\n"
                        "  shift\ndone\nexit 1\n")
        stub.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.stub}:{self.env['PATH']}", B71_PYTHON="skip",
                        B71_UV="skip", B71_FIXTURE_ARCHIVE=str(archive))

    def run_bootstrap(self):
        return subprocess.run(["sh", "bootstrap.sh"], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_install_reuse_and_corrupt_install_preservation(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((self.target / "bin" / "node").exists())
        (self.stub / "curl").write_text("#!/bin/sh\nexit 99\n")
        self.assertEqual(self.run_bootstrap().returncode, 0)
        (self.target / "bin" / "node").write_text("tampered")
        failure = self.run_bootstrap()
        self.assertEqual(failure.returncode, 2)
        self.assertIn("failed verification", failure.stderr)
        self.assertEqual((self.target / "bin" / "node").read_text(), "tampered")

    def test_bad_download_leaves_no_install_or_stage(self):
        (self.stub / "curl").write_text("#!/bin/sh\nexit 99\n")
        failure = self.run_bootstrap()
        self.assertEqual(failure.returncode, 2)
        self.assertFalse(self.target.exists())
        self.assertFalse(list((self.root / ".cache" / "host-tools").glob(".stage.*")))


if __name__ == "__main__":
    unittest.main()
