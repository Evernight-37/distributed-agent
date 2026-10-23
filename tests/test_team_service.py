import tempfile, unittest
from pathlib import Path
from unittest.mock import patch, Mock
from common import write_json
from team_service import TeamService, TARGET, run_cli, cli_environment

HOST = "laptop.example.ts.net"


def config():
    return {"TCP": {"443": {"HTTPS": True}}, "Web": {HOST + ":443": {"Handlers": {"/": {"Proxy": TARGET}}}}}


class Runner:
    def __init__(self, value=None):
        self.config = value or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args == ["status", "--json"]:
            return {"BackendState": "Running", "Self": {"DNSName": HOST + "."}}
        if args == ["serve", "status", "--json"]:
            return self.config
        if args == ["serve", "--bg", "--https=443", "off"]:
            self.config["TCP"].pop("443", None)
            self.config["Web"].pop(HOST + ":443", None)
            return {}
        raise AssertionError(args)


class TeamServiceTests(unittest.TestCase):
    def test_macos_calls_force_cli_mode_without_changing_parent_environment(self):
        with (
            patch("team_service.sys.platform", "darwin"),
            patch.dict("os.environ", {"TAILSCALE_BE_CLI": "0"}),
            patch("team_service.executable", return_value="/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
            patch(
                "team_service.subprocess.run",
                return_value=Mock(returncode=0, stdout='{"BackendState":"Running"}', stderr=""),
            ) as run,
        ):
            self.assertEqual(run_cli(["status", "--json"])["BackendState"], "Running")
            self.assertEqual(run.call_args.kwargs["env"]["TAILSCALE_BE_CLI"], "1")
            self.assertEqual(cli_environment()["TAILSCALE_BE_CLI"], "1")
            self.assertEqual(__import__("os").environ["TAILSCALE_BE_CLI"], "0")

    def test_unreadable_json_never_counts_as_an_empty_serve_configuration(self):
        for output in ("", "   ", "The Tailscale GUI failed to start", "[]", "null"):
            with (
                self.subTest(output=output),
                patch("team_service.executable", return_value="tailscale"),
                patch("team_service.subprocess.run", return_value=Mock(returncode=0, stdout=output, stderr="")),
            ):
                with self.assertRaisesRegex(RuntimeError, "Tailscale"):
                    run_cli(["serve", "status", "--json"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_adopt_and_no_create_mutation(self):
        runner = Runner(config())
        service = TeamService(self.root, TARGET, runner)
        self.assertEqual(service.status()["state"], "ready")
        self.assertEqual(service.create()["url"], "https://" + HOST)
        self.assertTrue(all("status" in cmd for cmd in runner.calls))

    def test_conflicts_never_modify(self):
        for change in ("other_proxy", "other_path", "public", "raw_tcp", "foreground"):
            c = config()
            if change == "other_proxy":
                c["Web"][HOST + ":443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
            if change == "other_path":
                c["Web"][HOST + ":443"]["Handlers"]["/other"] = {"Text": "other"}
            if change == "public":
                c["AllowFunnel"] = {HOST + ":443": True}
            if change == "raw_tcp":
                c["TCP"]["443"] = {"TCPForward": "localhost:9"}
            if change == "foreground":
                c["Foreground"] = {"other": {}}
            runner = Runner(c)
            service = TeamService(self.root, TARGET, runner)
            self.assertEqual(service.status()["state"], "conflict")
            with self.assertRaises(ValueError):
                service.create()
            with self.assertRaises(PermissionError):
                service.stop()
            self.assertTrue(all("status" in cmd for cmd in runner.calls))

    def test_orphan_funnel_and_malformed_listener_block_creation(self):
        for c in ({"AllowFunnel": {HOST + ":443": True}}, {"TCP": {"443": {}}}):
            service = TeamService(self.root, TARGET, Runner(c))
            self.assertEqual(service.status()["state"], "conflict")
            with self.assertRaises(ValueError):
                service.create()

    def test_stop_only_our_port(self):
        c = config()
        c["TCP"]["8443"] = {"HTTPS": True}
        c["Web"][HOST + ":8443"] = {"Handlers": {"/": {"Text": "other"}}}
        runner = Runner(c)
        service = TeamService(self.root, TARGET, runner)
        self.assertEqual(service.stop()["state"], "disabled")
        self.assertIn("8443", runner.config["TCP"])
        self.assertIn(HOST + ":8443", runner.config["Web"])

    def test_remote_no_configuration(self):
        for url in ("http://127.0.0.1:18876", "https://other.example.ts.net"):
            service = TeamService(self.root, url, Runner(config()))
            self.assertFalse(service.status()["can_manage"])
            with self.assertRaises(PermissionError):
                service.create()
            with self.assertRaises(PermissionError):
                service.stop()

    def test_failure_closed_when_status_unreadable(self):
        service = TeamService(self.root, TARGET, Mock(side_effect=RuntimeError("unreadable")))
        self.assertEqual(service.status()["state"], "error")
        with patch("team_service.subprocess.Popen") as popen:
            with self.assertRaises(ValueError):
                service.create()
            popen.assert_not_called()

    def test_authorization_and_duplicate_launch(self):
        service = TeamService(self.root, TARGET, Runner())
        write_json(
            service.job,
            {
                "state": "needs_authorization",
                "authorization_url": "https://evil.example/f/serve",
                "updated": __import__("time").time(),
            },
        )
        with patch("team_service.webbrowser.open") as browser:
            with self.assertRaises(ValueError):
                service.authorize()
            browser.assert_not_called()
        url = "https://login.tailscale.com/f/serve?node=example-id"
        write_json(
            service.job,
            {"state": "needs_authorization", "authorization_url": url, "updated": __import__("time").time()},
        )
        with patch("team_service.webbrowser.open") as browser, patch("team_service.subprocess.Popen") as popen:
            self.assertEqual(service.create()["state"], "needs_authorization")
            popen.assert_not_called()
            service.authorize()
            browser.assert_called_once_with(url)

    def test_launch_uses_sidecar_background_fixed_target(self):
        service = TeamService(self.root, TARGET, Runner())
        with patch("team_service.subprocess.Popen") as popen, patch("team_service.threading.Thread"):
            self.assertEqual(service.create()["state"], "starting")
            args = popen.call_args.args[0]
            self.assertIn("--create", args)
            self.assertIn(TARGET, args)
            self.assertNotIn("--yes", args)

    def test_mutation_success_does_not_require_json(self):
        with (
            patch("team_service.executable", return_value="tailscale.exe"),
            patch("team_service.subprocess.run", return_value=Mock(returncode=0, stdout="Serve disabled\n", stderr="")),
        ):
            self.assertEqual(run_cli(["serve", "--bg", "--https=443", "off"]), {})

    def test_missing_and_not_logged_in(self):
        service = TeamService(self.root, TARGET, Mock(side_effect=FileNotFoundError("missing")))
        self.assertEqual(service.status()["state"], "missing")
        service = TeamService(self.root, TARGET, lambda args: {"BackendState": "NeedsLogin"})
        self.assertEqual(service.status()["state"], "needs_login")


if __name__ == "__main__":
    unittest.main()
