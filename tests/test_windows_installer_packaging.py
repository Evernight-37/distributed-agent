import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsInstallerPackagingTests(unittest.TestCase):
    def test_inno_script_is_per_user_and_keeps_data(self):
        script = (ROOT / "installer" / "windows" / "AgentHarness.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}\\AgentHarness\\app", script)
        self.assertIn("DisableDirPage=yes", script)
        self.assertIn("OutputDir=..\\..", script)
        self.assertIn('Name: "{autoprograms}\\{#AppName}"', script)
        self.assertNotIn('AppId "AgentHarness-" + MyAppVersion', script)
        self.assertNotIn("\\versions\\{#MyAppVersion}", script)
        self.assertNotIn("[UninstallDelete]", script)
        self.assertNotIn('filesandordirs; Name: "{app}"', script)
        self.assertNotIn("[Code]", script)
        self.assertNotIn("InitializeSetup", script)

    def test_release_workflow_keeps_simple_unsigned_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("AgentHarness-$version-windows-x64-setup.exe", workflow)
        self.assertIn("package_windows_release.py", workflow)
        self.assertIn(
            "python -m pip install -e . -r requirements-linux-build.txt ruff==0.14.1 pywebview==6.2.1",
            workflow,
        )
        self.assertNotIn("WINDOWS_SIGNING_CERT_B64", workflow)
        self.assertNotIn("signtool", workflow)
        self.assertNotIn("anchore/sbom-action", workflow)
        self.assertNotIn("release-manifest", workflow)


if __name__ == "__main__":
    unittest.main()
