"""Offline checks for the selected deployment's content logging opt-in."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/ai/litellm/app"
MODULE = APP / "callbacks/local_model_content_logging.py"


class LoggingObject:
    def __init__(self, details=None, own=None):
        self.model_call_details = (
            {"standard_callback_dynamic_params": details} if details is not ... else {}
        )
        self.standard_callback_dynamic_params = own


class ContentLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = ("litellm", "litellm.integrations", "litellm.integrations.custom_logger")
        cls.old = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
        spec = importlib.util.spec_from_file_location("local_model_content_logging_test", MODULE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        for name, old in cls.old.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    def run_hook(self, request):
        result = asyncio.run(self.module.proxy_handler_instance.async_pre_call_deployment_hook(request, "acompletion"))
        self.assertIs(result, request)

    def request(self, model_info=None, logging_obj=None):
        return {
            "model_info": {"id": "selected-1", "log_message_content": True} if model_info is None else model_info,
            "litellm_logging_obj": LoggingObject(details={}) if logging_obj is None else logging_obj,
        }

    def test_flag_true_enables_content(self):
        details = {"existing": "value"}
        own = {"other": "value"}
        request = self.request(logging_obj=LoggingObject(details=details, own=own))
        self.run_hook(request)
        self.assertIs(request["litellm_logging_obj"].model_call_details["standard_callback_dynamic_params"], details)
        self.assertIs(details["turn_off_message_logging"], False)
        self.assertIs(own["turn_off_message_logging"], False)

    def test_only_boolean_flag_and_selected_id_enable_content(self):
        for info in (
            {"id": "selected-1"},
            {"id": "selected-1", "log_message_content": False},
            {"id": "selected-1", "log_message_content": "true"},
            {"log_message_content": True},
            {"id": "", "log_message_content": True},
            "untrusted",
        ):
            with self.subTest(info=info):
                logging_obj = LoggingObject(details={})
                request = self.request(model_info=info, logging_obj=logging_obj)
                self.run_hook(request)
                self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})

    def test_missing_logging_object_stays_unchanged(self):
        request = self.request()
        del request["litellm_logging_obj"]
        self.run_hook(request)

    def test_none_dynamic_params_are_created(self):
        logging_obj = LoggingObject(details=None, own=None)
        request = self.request(logging_obj=logging_obj)
        self.run_hook(request)
        params = logging_obj.model_call_details["standard_callback_dynamic_params"]
        self.assertIs(params, logging_obj.standard_callback_dynamic_params)
        self.assertIs(params["turn_off_message_logging"], False)

    def test_spoofed_metadata_cannot_opt_in(self):
        for field in ("metadata", "litellm_metadata"):
            with self.subTest(field=field):
                logging_obj = LoggingObject(details={})
                self.run_hook(self.request(logging_obj=logging_obj))
                request = self.request(model_info={"id": "cloud-1"}, logging_obj=logging_obj)
                request[field] = {"model_group": "dub-text", "log_message_content": True}
                self.run_hook(request)
                self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})
                self.run_hook(self.request(logging_obj=logging_obj))
                self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})

    def test_flagged_then_unflagged_stays_redacted(self):
        logging_obj = LoggingObject(details={}, own={})
        self.run_hook(self.request(logging_obj=logging_obj))
        for params in (logging_obj.model_call_details["standard_callback_dynamic_params"],
                       logging_obj.standard_callback_dynamic_params):
            self.assertIs(params["turn_off_message_logging"], False)
        self.run_hook(self.request(model_info={"id": "cloud-1"}, logging_obj=logging_obj))
        self.run_hook(self.request(logging_obj=logging_obj))
        for params in (logging_obj.model_call_details["standard_callback_dynamic_params"],
                       logging_obj.standard_callback_dynamic_params):
            self.assertNotIn("turn_off_message_logging", params)

    def test_unflagged_then_flagged_stays_redacted(self):
        logging_obj = LoggingObject(details={})
        self.run_hook(self.request(model_info={"id": "cloud-1"}, logging_obj=logging_obj))
        self.run_hook(self.request(logging_obj=logging_obj))
        self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})

    def test_existing_dynamic_value_prevents_opt_in_without_latching(self):
        for existing in (True, False, "external"):
            for location in ("details", "own"):
                with self.subTest(existing=existing, location=location):
                    details = {"turn_off_message_logging": existing} if location == "details" else {}
                    own = {"turn_off_message_logging": existing} if location == "own" else {}
                    logging_obj = LoggingObject(details=details, own=own)
                    self.run_hook(self.request(logging_obj=logging_obj))
                    self.assertEqual(details, {"turn_off_message_logging": existing} if location == "details" else {})
                    self.assertEqual(own, {"turn_off_message_logging": existing} if location == "own" else {})
                    self.assertEqual(logging_obj._local_model_content_logging_state["changes"], [])
                    self.assertIs(logging_obj._local_model_content_logging_state["blocked"], False)
                    (details if location == "details" else own).pop("turn_off_message_logging")
                    self.run_hook(self.request(logging_obj=logging_obj))
                    self.assertIs(details["turn_off_message_logging"], False)
                    self.assertIs(own["turn_off_message_logging"], False)

    def test_unflagged_deployment_preserves_unowned_value(self):
        logging_obj = LoggingObject(details={"turn_off_message_logging": "external"})
        self.run_hook(self.request(model_info={"id": "cloud-1"}, logging_obj=logging_obj))
        self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"]["turn_off_message_logging"], "external")

    def test_existing_own_value_does_not_create_details_params(self):
        logging_obj = LoggingObject(details=None, own={"turn_off_message_logging": True})
        self.run_hook(self.request(logging_obj=logging_obj))
        self.assertIsNone(logging_obj.model_call_details["standard_callback_dynamic_params"])
        self.assertIs(logging_obj.standard_callback_dynamic_params["turn_off_message_logging"], True)

    def test_misbehaving_logging_object_does_not_raise(self):
        class Broken:
            @property
            def model_call_details(self):
                raise RuntimeError("synthetic failure")

        request = self.request(logging_obj=Broken())
        self.run_hook(request)

    def test_partial_update_is_rolled_back(self):
        class BrokenParams(dict):
            def __setitem__(self, key, value):
                if key == "turn_off_message_logging":
                    raise RuntimeError("synthetic failure")
                super().__setitem__(key, value)

        logging_obj = LoggingObject(details={}, own=BrokenParams())
        self.run_hook(self.request(logging_obj=logging_obj))
        self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})
        self.run_hook(self.request(logging_obj=logging_obj))
        self.assertEqual(logging_obj.model_call_details["standard_callback_dynamic_params"], {})

    def test_config_opt_in_is_exactly_local_models(self):
        outer = yaml.safe_load((APP / "configmap.yaml").read_text())
        config = yaml.safe_load(outer["data"]["config.yaml"])
        flagged = {entry["model_name"] for entry in config["model_list"]
                   if entry.get("model_info", {}).get("log_message_content") is True}
        self.assertEqual(flagged, {"deepseek-v4.1-flash", "dub-text"})
        for entry in config["model_list"]:
            self.assertNotIn("turn_off_message_logging", entry["litellm_params"])
        callbacks = config["litellm_settings"]["callbacks"]
        self.assertEqual(callbacks[callbacks.index("langfuse_otel") - 1],
                         "local_model_content_logging.proxy_handler_instance")
        self.assertIs(config["litellm_settings"]["turn_off_message_logging"], True)
        kustomization = yaml.safe_load((APP / "kustomization.yaml").read_text())
        self.assertIn("local_model_content_logging.py=callbacks/local_model_content_logging.py",
                      kustomization["configMapGenerator"][0]["files"])


if __name__ == "__main__":
    unittest.main()
