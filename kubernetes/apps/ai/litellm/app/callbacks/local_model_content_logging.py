"""Enable content tracing after deployment selection, when trusted model_info is available.

Only the selected deployment's model_info may override the proxy's global
redaction policy; request fields can be supplied by clients.
Validated against LiteLLM 1.108.0; re-validate on upgrade because this hook fails silent.
The x-litellm-enable-message-redaction header is not checked here and can be overridden.
"""

from litellm.integrations.custom_logger import CustomLogger


class LocalModelContentLogging(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        state = None
        try:
            logging_obj = kwargs.get("litellm_logging_obj")
            if logging_obj is None:
                return kwargs

            state = getattr(logging_obj, "_local_model_content_logging_state", None)
            if state is None:
                state = {"blocked": False, "changes": []}
                logging_obj._local_model_content_logging_state = state
            if not isinstance(state, dict):
                return kwargs

            model_info = kwargs.get("model_info")
            flagged = (isinstance(model_info, dict)
                       and model_info.get("log_message_content") is True
                       and bool(model_info.get("id")))
            if not flagged:
                state["blocked"] = True
                for params in state["changes"]:
                    if params.get("turn_off_message_logging") is False:
                        params.pop("turn_off_message_logging", None)
                state["changes"].clear()
                return kwargs
            if state["blocked"]:
                return kwargs

            details = logging_obj.model_call_details
            if not isinstance(details, dict):
                return kwargs

            params = details.get("standard_callback_dynamic_params")
            if params is not None and not isinstance(params, dict):
                return kwargs

            absent = object()
            own_params = getattr(logging_obj, "standard_callback_dynamic_params", absent)
            if own_params is not absent and own_params is not None and not isinstance(own_params, dict):
                return kwargs

            targets = [params] if params is not None else []
            if isinstance(own_params, dict) and own_params is not params:
                targets.append(own_params)
            if any("turn_off_message_logging" in target for target in targets):
                return kwargs

            if params is None:
                params = {}
                if own_params is not absent and own_params is None:
                    logging_obj.standard_callback_dynamic_params = params
                details["standard_callback_dynamic_params"] = params
                targets.insert(0, params)
            elif own_params is None:
                logging_obj.standard_callback_dynamic_params = params

            for target in targets:
                state["changes"].append(target)
                target["turn_off_message_logging"] = False
        except Exception:
            if isinstance(state, dict):
                try:
                    state["blocked"] = True
                    for params in state["changes"]:
                        if params.get("turn_off_message_logging") is False:
                            params.pop("turn_off_message_logging", None)
                    state["changes"].clear()
                except Exception:
                    pass
        return kwargs


proxy_handler_instance = LocalModelContentLogging()
