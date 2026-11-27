import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
SOURCE = APP / "deploy/pilot"
if not (SOURCE / "check_certificate.py").is_file():
    SOURCE = APP.parent / "setup"
PS51 = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and PS51.is_file(), "Windows certificate setup uses Windows PowerShell 5.1 / .NET")
class CertificateSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="platelog TLS test ")
        cls.root = Path(cls.temporary.name).resolve()
        def cleanup():
            assert cls.root.is_relative_to(Path(tempfile.gettempdir()).resolve()) and cls.root.name.startswith("platelog TLS test ")
            cls.temporary.cleanup()
        cls.addClassCleanup(cleanup)
        runner = APP / "tests/certificate_setup_cases.ps1"
        # A Python child of PowerShell 7 otherwise inherits Core-only module paths.
        environment = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
        result = subprocess.run([str(PS51), "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(runner),
                                 "-Source", str(SOURCE), "-Python", sys.executable, "-Root", str(cls.root)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=120, env=environment)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.cases = json.loads((cls.root / "cases.json").read_text(encoding="utf-8-sig"))

    def test_default_choice_is_it_managed_and_lifetime_is_selectable(self):
        self.assertEqual(self.cases["default_menu"], "ITManaged")
        self.assertEqual(self.cases["fallback_menu"], "PlateLogGenerated")
        self.assertEqual(self.cases["chosen_days"], 90)
        self.assertEqual(self.cases["default"]["LifetimeDays"], 365)
        self.assertEqual(self.cases["generated"]["LifetimeDays"], 180)
        expires = datetime.fromisoformat(self.cases["generated"]["ExpiresUtc"])
        self.assertAlmostEqual((expires - datetime.now(timezone.utc)).total_seconds() / 86400, 180, delta=.02)

    def test_it_managed_mode_uses_supplied_expiry_and_preserves_files(self):
        self.assertEqual(self.cases["managed"]["Mode"], "ITManaged")
        self.assertEqual(self.cases["managed"]["ExpiresUtc"], self.cases["generated"]["ExpiresUtc"])
        self.assertIsNone(self.cases["managed"]["LifetimeDays"])
        self.assertEqual(self.cases["managed"]["PrivateKeyPath"], self.cases["generated"]["PrivateKeyPath"])

    def test_validation_rejects_wrong_name_untrusted_certificate_and_key(self):
        case = self.cases["generated"]
        command = [sys.executable, "-I", "-B", str(SOURCE / "check_certificate.py"), "--certificate", case["CertificatePath"],
                   "--private-key", case["PrivateKeyPath"], "--server-name"]
        for name, trust in (("wrong.example.internal", case["TrustBundlePath"]), ("platelog.example.internal", None)):
            with self.subTest(name=name, trusted=bool(trust)):
                args = command + [name] + (["--trust-ca", trust] if trust else [])
                result = subprocess.run(args, capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stderr)["error_type"], "SSLCertVerificationError")
        self.assertTrue(self.cases["failed_validation_preserved"])

    def test_renewal_keeps_previous_material_and_changes_public_certificate(self):
        self.assertEqual(self.cases["renewed"]["LifetimeDays"], 90)
        self.assertNotEqual(self.cases["renewed"]["CertificateSHA256"], self.cases["generated"]["CertificateSHA256"])
        self.assertTrue(self.cases["old_key_preserved"])

    def test_keys_are_restricted_and_windows_trust_store_is_unchanged(self):
        self.assertEqual(self.cases["rsa_bits"], 3072)
        self.assertTrue(self.cases["key_runtime_read_only"])
        self.assertTrue(self.cases["windows_store_unchanged"])

    def test_unsafe_server_names_are_rejected(self):
        self.assertEqual(self.cases["invalid_names_rejected"], 5)

    def test_expired_future_missing_san_and_wrong_purpose_are_rejected(self):
        self.assertEqual(self.cases["invalid_certificates"], {name: True for name in ("expired", "future", "missing-san", "wrong-purpose")})

    def test_admin_bootstrap_accepts_stdin_and_refuses_to_reset_credentials(self):
        from platelog.credentials import password_matches
        from platelog.store import Store
        data = self.root / "bootstrap-data"
        password = "Synthetic setup passphrase \"quoted\" \\ unicode \u00e9!"
        args = [sys.executable, "-I", "-B", str(SOURCE / "bootstrap_admin.py"), "--app-dir", str(APP), "--data-dir", str(data)]
        first = subprocess.run(args, input=json.dumps({"password": password}).encode(), capture_output=True, timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        original = Store(data).setting("admin_password")
        self.assertTrue(password_matches(password, original))
        second = subprocess.run(args, input=json.dumps({"password": "Different synthetic passphrase!"}).encode(), capture_output=True, timeout=20)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(Store(data).setting("admin_password"), original)
        self.assertNotIn(password.encode(), first.stdout + first.stderr + second.stdout + second.stderr)


if __name__ == "__main__":
    unittest.main()
