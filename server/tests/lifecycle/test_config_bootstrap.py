from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigurationBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parents[3]
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(self.repository / "server")
        for key in ("JWT_SECRET", "SECRET_ENCRYPTION_KEY", "RCLONE_CONFIG_PASS"):
            environment.pop(key, None)
        prefix = (
            "import sys,types\n"
            "dotenv=types.ModuleType('dotenv')\n"
            "dotenv.load_dotenv=lambda *a,**k: False\n"
            "sys.modules['dotenv']=dotenv\n"
        )
        return subprocess.run(
            [sys.executable, "-c", prefix + body],
            cwd=self.repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_config_import_does_not_generate_secrets_or_files(self) -> None:
        target = self.temp / "server.env"
        result = self._run(
            "import config\n"
            f"config.settings.SERVER_ENV_PATH={str(target)!r}\n"
            "print(bool(config.settings.JWT_SECRET), bool(config.settings.SECRET_ENCRYPTION_KEY), "
            "bool(config.settings.RCLONE_CONFIG_PASS))\n"
            f"print({str(target)!r}, __import__('pathlib').Path({str(target)!r}).exists())\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("False False False", result.stdout)
        self.assertIn("False", result.stdout.splitlines()[-1])
        self.assertFalse(target.exists())

    def test_guarded_initialization_fails_without_writing(self) -> None:
        target = self.temp / "guarded.env"
        result = self._run(
            "import config\n"
            f"config.settings.SERVER_ENV_PATH={str(target)!r}\n"
            "try:\n"
            " config.initialize_runtime_configuration(allow_mutation=False)\n"
            "except config.ConfigurationInitializationError as exc:\n"
            " print(exc.code)\n"
            f"print(__import__('pathlib').Path({str(target)!r}).exists())\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["configuration_initialization_failed", "False"])
        self.assertFalse(target.exists())

    def test_normal_initialization_generates_all_secrets_explicitly(self) -> None:
        target = self.temp / "normal.env"
        result = self._run(
            "import config,json\n"
            f"config.settings.SERVER_ENV_PATH={str(target)!r}\n"
            "result=config.initialize_runtime_configuration(allow_mutation=True)\n"
            "print(json.dumps(sorted(result.generated_keys)))\n"
            f"print(__import__('pathlib').Path({str(target)!r}).is_file())\n"
            "print(all(bool(getattr(config.settings,key)) for key in result.generated_keys))\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.splitlines()[0]),
            ["JWT_SECRET", "RCLONE_CONFIG_PASS", "SECRET_ENCRYPTION_KEY"],
        )
        self.assertEqual(result.stdout.splitlines()[1:], ["True", "True"])
        self.assertTrue(target.is_file())
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_private_temp_file_is_mode_0600_before_content_is_written(self) -> None:
        target = self.temp / "private.env"
        result = self._run(
            "import config,os,stat\n"
            "seen=[]\n"
            "real_write=config.os.write\n"
            "def checked_write(fd,data):\n"
            " seen.append(stat.S_IMODE(os.fstat(fd).st_mode))\n"
            " return real_write(fd,data)\n"
            "config.os.write=checked_write\n"
            f"config._append_private_env_values({str(target)!r}, {{'JWT_SECRET':'secret'}})\n"
            "print(seen)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        if os.name != "nt":
            self.assertEqual(json.loads(result.stdout.replace("'", '"')), [0o600])

    def test_private_atomic_replace_completes_short_writes(self) -> None:
        target = self.temp / "short-write.env"
        result = self._run(
            "import config,os\n"
            "real_write=config.os.write\n"
            "calls=[]\n"
            "def short_write(fd,data):\n"
            " limit=max(1,len(data)//2)\n"
            " calls.append(limit)\n"
            " return real_write(fd,data[:limit])\n"
            "config.os.write=short_write\n"
            f"config._append_private_env_values({str(target)!r}, {{'JWT_SECRET':'complete-secret'}})\n"
            f"print(open({str(target)!r}, encoding='utf-8').read().strip())\n"
            "print(len(calls))\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], 'JWT_SECRET="complete-secret"')
        self.assertGreater(int(result.stdout.splitlines()[1]), 1)

    def test_private_atomic_replace_cleans_temp_files_on_failures(self) -> None:
        for operation in ("write", "zero_write", "fsync", "replace"):
            with self.subTest(operation=operation):
                target = self.temp / f"failure-{operation}.env"
                result = self._run(
                    "import config,os,pathlib\n"
                    f"target=pathlib.Path({str(target)!r})\n"
                    f"operation={operation!r}\n"
                    "if operation == 'write':\n"
                    " config.os.write=lambda *a,**k: (_ for _ in ()).throw(OSError('write failed'))\n"
                    "elif operation == 'zero_write':\n"
                    " config.os.write=lambda *a,**k: 0\n"
                    "elif operation == 'fsync':\n"
                    " config.os.fsync=lambda *a,**k: (_ for _ in ()).throw(OSError('fsync failed'))\n"
                    "else:\n"
                    " config.os.replace=lambda *a,**k: (_ for _ in ()).throw(OSError('replace failed'))\n"
                    "try:\n"
                    " config._append_private_env_values(str(target), {'JWT_SECRET':'secret'})\n"
                    "except OSError:\n"
                    " pass\n"
                    "else:\n"
                    " raise AssertionError('failure injection did not fail')\n"
                    "print(target.exists(), sorted(p.name for p in target.parent.glob(target.name + '.*.tmp')))\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "False []")

    def test_legacy_key_removal_uses_private_atomic_replacement(self) -> None:
        target = self.temp / "legacy.env"
        target.write_text('KEEP="yes"\nAPI_BEARER_TOKEN="secret"\n', encoding="utf-8")
        result = self._run(
            "import config,os,stat\n"
            "seen=[]\n"
            "real_write=config.os.write\n"
            "def checked_write(fd,data):\n"
            " seen.append(stat.S_IMODE(os.fstat(fd).st_mode))\n"
            " return real_write(fd,data)\n"
            "config.os.write=checked_write\n"
            f"config.remove_env_key({str(target)!r}, 'API_BEARER_TOKEN')\n"
            f"print(open({str(target)!r}, encoding='utf-8').read().strip())\n"
            "print(seen)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], 'KEEP="yes"')
        if os.name != "nt":
            self.assertEqual(result.stdout.splitlines()[1], "[384]")


if __name__ == "__main__":
    unittest.main()
