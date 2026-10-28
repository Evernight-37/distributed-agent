import json
import unittest

from agent_harness.api_contracts import (
    API_CONTRACT_VERSION,
    DESKTOP_CONTROL,
    HUB_ACCOUNT,
    NODE_EXECUTION,
    BoundaryContract,
)
from agent_harness.desktop_actions import (
    DesktopIdentityActions,
    DesktopOwnerActions,
    DesktopProjectActions,
)
from agent_harness.desktop_api import (
    DesktopControlAPI,
    DesktopSessionActions,
    DesktopSettingsActions,
    DesktopTeamServiceActions,
)
from agent_harness.hub_api import HubAccountAPI
from agent_harness.node_api import NodeExecutionAPI


class BoundaryContractTests(unittest.TestCase):
    def test_contract_descriptions_are_stable_machine_readable_objects(self):
        for contract in (DESKTOP_CONTROL, NODE_EXECUTION, HUB_ACCOUNT):
            with self.subTest(contract=contract.name):
                description = contract.describe()
                self.assertEqual(description["version"], API_CONTRACT_VERSION)
                self.assertEqual(description["actions"], sorted(contract.actions))
                json.dumps(description)

    def test_common_wire_shape_rejects_unknown_or_non_object_requests(self):
        contract = BoundaryContract(
            "test",
            frozenset({"known"}),
            required_fields={"known": frozenset({"value"})},
        )
        with self.assertRaisesRegex(ValueError, "Unknown test action"):
            contract.validate("unknown", {})
        with self.assertRaisesRegex(ValueError, "payload must be an object"):
            contract.validate("known", [])
        with self.assertRaisesRegex(ValueError, "payload keys must be strings"):
            contract.validate("known", {1: "value"})
        with self.assertRaisesRegex(ValueError, "request must be an object"):
            contract.validate_request("known")
        with self.assertRaisesRegex(ValueError, "requires fields: value"):
            contract.validate("known", {})
        self.assertEqual(contract.validate("known", {"value": 1}), ("known", {"value": 1}))

    def test_contract_schema_is_validated_and_immutable(self):
        with self.assertRaisesRegex(ValueError, "schemas for unknown actions"):
            BoundaryContract("test", frozenset({"known"}), {"other": frozenset({"value"})})
        contract = BoundaryContract("test", frozenset({"known"}), {"known": frozenset({"value"})})
        with self.assertRaises(TypeError):
            contract.required_fields["known"] = frozenset()

    def test_boundaries_validate_before_calling_business_handlers(self):
        calls = []

        def action_handler(action, payload):
            calls.append((action, payload))
            return "desktop"

        def request_handler(request):
            calls.append(request)
            return "request"

        desktop = DesktopControlAPI(action_handler)
        node = NodeExecutionAPI(request_handler)
        account = HubAccountAPI(request_handler)

        self.assertEqual(desktop.invoke("dashboard"), "desktop")
        self.assertEqual(node.dispatch({"action": "status", "job": "j"}), "request")
        self.assertEqual(account.dispatch({"action": "challenge", "public_key": "k"}), "request")
        accepted = len(calls)

        for api, request in (
            (node, {"action": "dashboard"}),
            (account, {"action": "status"}),
        ):
            with self.subTest(api=type(api).__name__):
                with self.assertRaises(ValueError):
                    api.dispatch(request)
        with self.assertRaises(ValueError):
            desktop.invoke("status")
        self.assertEqual(len(calls), accepted)

    def test_node_and_account_contracts_reject_missing_boundary_fields(self):
        node = NodeExecutionAPI(lambda request: request)
        account = HubAccountAPI(lambda request: request)
        with self.assertRaisesRegex(ValueError, "requires fields: job"):
            node.dispatch({"action": "fetch"})
        with self.assertRaisesRegex(ValueError, "requires fields: envelope"):
            account.dispatch({"action": "rpc"})

    def test_desktop_contract_rejects_missing_boundary_fields(self):
        desktop = DesktopControlAPI(lambda action, payload: payload)
        with self.assertRaisesRegex(ValueError, "requires fields: profile"):
            desktop.invoke("login")
        self.assertEqual(desktop.invoke("login", {"profile": "p"}), {"profile": "p"})
        with self.assertRaisesRegex(ValueError, "requires fields: token"):
            desktop.invoke("team-service-restore-apply")
        with self.assertRaisesRegex(ValueError, "requires fields: base64, name, project"):
            desktop.invoke("attachment-upload")

    def test_migrated_desktop_settings_and_service_groups_keep_login_boundary(self):
        class LoggedOutBridge:
            profile = None
            identity = None

        bridge = LoggedOutBridge()
        with self.assertRaisesRegex(PermissionError, "请先登录"):
            DesktopSettingsActions(bridge).invoke("settings-get", {})
        with self.assertRaisesRegex(PermissionError, "请先登录"):
            DesktopTeamServiceActions(bridge).invoke("team-service-status", {})
        with self.assertRaisesRegex(PermissionError, "请先登录"):
            DesktopOwnerActions(bridge).invoke("device-inspect", {"code": "x"})
        with self.assertRaisesRegex(PermissionError, "请先登录"):
            DesktopProjectActions(bridge).invoke("dashboard", {})

    def test_desktop_action_groups_are_disjoint_and_cover_contract(self):
        groups = (
            DesktopSessionActions,
            DesktopSettingsActions,
            DesktopTeamServiceActions,
            DesktopIdentityActions,
            DesktopOwnerActions,
            DesktopProjectActions,
        )
        owner = {}
        for group in groups:
            for action in group.actions:
                self.assertNotIn(action, owner, f"{action} is owned by two Desktop groups")
                owner[action] = group.__name__
        self.assertEqual(set(owner), set(DESKTOP_CONTROL.actions))

    def test_node_execution_actions_use_dedicated_handler(self):
        calls = []
        node = NodeExecutionAPI(
            lambda request: calls.append(("core", request)) or "core",
            lambda request: calls.append(("execution", request)) or "execution",
        )
        request = {
            "action": "execution_start",
            "project": "p",
            "execution": "e",
            "template": "tests",
        }
        self.assertEqual(node.dispatch(request), "execution")
        self.assertEqual(calls, [("execution", request)])

    def test_node_action_groups_run_before_core_handler(self):
        calls = []

        class StatusGroup:
            actions = frozenset({"status"})

            def dispatch(self, request):
                calls.append(("group", request))
                return "group"

        node = NodeExecutionAPI(
            lambda request: calls.append(("core", request)) or "core",
            action_groups=(StatusGroup(),),
        )
        request = {"action": "status", "job": "j"}
        self.assertEqual(node.dispatch(request), "group")
        self.assertEqual(calls, [("group", request)])

    def test_field_schemas_are_serializable_and_validate_transport_bounds(self):
        for contract in (DESKTOP_CONTROL, NODE_EXECUTION, HUB_ACCOUNT):
            description = contract.describe()
            json.dumps(description)
            self.assertIn("field_schemas", description)
        with self.assertRaisesRegex(ValueError, "too long"):
            DESKTOP_CONTROL.validate("message", {"project": "p", "body": "x" * 20001})
        with self.assertRaisesRegex(ValueError, "invalid value"):
            NODE_EXECUTION.validate(
                "configure",
                {"project": "p", "model": "m", "effort": "invalid", "timeout": 60},
            )
        with self.assertRaisesRegex(ValueError, "must be integer"):
            NODE_EXECUTION.validate(
                "configure",
                {"project": "p", "model": "m", "effort": "high", "timeout": "60"},
            )
        with self.assertRaisesRegex(ValueError, "too short"):
            DESKTOP_CONTROL.validate("recovery-export", {"recovery_password": ""})


if __name__ == "__main__":
    unittest.main()
