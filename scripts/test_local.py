"""Safe automation scenarios; all mutable state is temporary."""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import local


class LocalAutomationTest(unittest.TestCase):
    def test_clean_removes_only_generated_caches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frontend = root / "frontend"
            backend = root / "backend"
            scripts = root / "scripts"
            for path in (frontend / "dist", frontend / "node_modules" / ".vite",
                         backend / "app" / "__pycache__", backend / "tests" / "__pycache__",
                         backend / ".venv", scripts / "__pycache__"):
                path.mkdir(parents=True)
            (root / ".env").write_text("private", encoding="utf-8")
            (frontend / "node_modules" / "keep").write_text("installed", encoding="utf-8")
            (frontend / "package.json").write_text(
                '{"scripts":{"clean":"node -e \\\"const fs=require(\'node:fs\'); for (const p of [\'dist\',\'node_modules/.vite\']) fs.rmSync(p,{recursive:true,force:true})\\\""}}',
                encoding="utf-8")
            (backend / "scripts").mkdir()
            shutil.copyfile(local.BACKEND / "scripts" / "clean_cache.py", backend / "scripts" / "clean_cache.py")
            with patch.object(local, "ROOT", root), patch.object(local, "FRONTEND", frontend), \
                 patch.object(local, "BACKEND", backend), patch.object(local, "preflight"):
                local.clean()
            for path in (frontend / "dist", frontend / "node_modules" / ".vite",
                         backend / "app" / "__pycache__", backend / "tests" / "__pycache__",
                         scripts / "__pycache__"):
                self.assertFalse(path.exists(), str(path))
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "private")
            self.assertTrue((frontend / "node_modules" / "keep").exists())
            self.assertTrue((backend / ".venv").exists())

    def test_failed_preflight_leaves_existing_state_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.example").write_text("example", encoding="utf-8")
            with patch.object(local, "ROOT", root), patch.object(local, "ENV", root / ".env"), \
                 patch.object(local, "preflight", side_effect=local.Failure("daemon unavailable", 2)), \
                 patch.object(local, "run") as command:
                with self.assertRaises(local.Failure):
                    local.setup()
                self.assertFalse((root / ".env").exists())
                command.assert_not_called()

    def test_repeat_setup_preserves_env_and_rechecks_both_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / ".env"
            env.write_text("private existing value", encoding="utf-8")
            calls = []

            def fake_compose(*args, **_kwargs):
                calls.append(args)
                if args[:2] == ("ps", "--format"):
                    return '{"Health":"healthy"}'
                if args[:2] == ("exec", "-T"):
                    return "1"
                return ""

            with patch.object(local, "ROOT", root), patch.object(local, "ENV", env), \
                 patch.object(local, "preflight"), patch.object(local, "run"), \
                 patch.object(local, "compose", side_effect=fake_compose), \
                 patch.object(local, "api_probe"), patch.object(local, "checks"):
                local.setup()
                local.setup()
            self.assertEqual(env.read_text(encoding="utf-8"), "private existing value")
            self.assertEqual(sum(call[:2] == ("up", "-d") for call in calls), 2)
            self.assertEqual(sum(call[:2] == ("exec", "-T") for call in calls), 4)

    @unittest.skipIf(os.name == "nt", "POSIX process-group verification")
    def test_stop_child_signals_its_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-stopped"
            grandchild = ("import signal,time,pathlib; "
                          f"p=pathlib.Path({str(marker)!r}); "
                          "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('stopped'), exit(0))); "
                          "time.sleep(30)")
            parent = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
                      "time.sleep(30)")
            child = local.spawn([sys.executable, "-c", parent], Path(temp))
            try:
                time.sleep(0.5)
                local.stop_child(child)
                for _ in range(20):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")
            finally:
                local.stop_child(child)

    @unittest.skipIf(os.name == "nt", "POSIX process-group verification")
    def test_stop_child_cleans_descendant_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "orphan-stopped"
            ready = Path(temp) / "descendant-ready"
            grandchild = ("import signal,time,pathlib; "
                          f"p=pathlib.Path({str(marker)!r}); r=pathlib.Path({str(ready)!r}); "
                          "signal.signal(signal.SIGTERM, lambda *_: (p.write_text('stopped'), exit(0))); "
                          "r.write_text('ready'); time.sleep(30)")
            parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grandchild!r}])"
            child = local.spawn([sys.executable, "-c", parent], Path(temp))
            try:
                child.wait(timeout=5)
                for _ in range(20):
                    if ready.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(ready.exists())
                local.stop_child(child)
                for _ in range(20):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "stopped")
            finally:
                local.stop_child(child)


if __name__ == "__main__":
    unittest.main()
