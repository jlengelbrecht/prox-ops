#!/usr/bin/env python3
"""Guarded, offline patch for the exact LiteLLM v1.108.0 image source."""
import argparse
import hashlib
from pathlib import Path

EXPECTED = {
    "proxy/anthropic_endpoints/endpoints.py": "76938a6592ef4d9a55b0579835960f49494aed26bc6374cf8b5a07aaff693951",
    "proxy/utils.py": "a456dc0f4b215cef52f165ec6b24357fca671916306e2e58fbc576b4f4d4b85e",
    "proxy/common_request_processing.py": "7aae910c7619c3445d843522a3dbfd8ba5347821c6e1c2673e2298125017cbe9",
    "router.py": "c872c93cd9e5b1ded324d44c9c5220d38febe61ddf52031bcb05c669d8b8e9f3",
    "llms/anthropic/pass_through/adapters/streaming_iterator.py": "eb16c8582806a25805fff9b51bccf8ad67c466709e2d4fdf6996986ebe07a138",
}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError("patch context mismatch")
    return source.replace(old, new, 1)


def patch_endpoint(source):
    source = replace_once(source,
        '''from litellm.integrations.custom_guardrail import ModifyResponseException\n''',
        '''from litellm.exceptions import CallerCredentialAuthenticationError\nfrom litellm.integrations.custom_guardrail import ModifyResponseException\n''')
    old = '''        await proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=base_llm_response_processor.data
        )
        log_llm_api_exception(e, base_llm_response_processor.litellm_call_id)
'''
    new = '''        transformed_exception = await proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=base_llm_response_processor.data
        )
        # This callback owns the fixed alias failure contract. If its replacement
        # disappears, close the alias error rather than returning provider text.
        request_data = base_llm_response_processor.data
        carrier = request_data.get("proxy_server_request") if isinstance(request_data, dict) else None
        carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
        is_grok_alias = (isinstance(request_data, dict) and request_data.get("model") == "project-grok-native") or (
            isinstance(carrier_body, dict) and carrier_body.get("model") == "project-grok-native"
        )
        # The frozen callback also returns a 502 for its own request denials.
        # Preserve their existing 401 response while selecting provider errors.
        is_alias_denial = (is_grok_alias and isinstance(e, CallerCredentialAuthenticationError)
            and getattr(e, "message", None) == "litellm.AuthenticationError: Project Grok request rejected")
        if is_alias_denial:
            pass
        elif transformed_exception is not None:
            e = transformed_exception
        elif is_grok_alias:
            e = HTTPException(status_code=502, detail="Project Grok provider failed")
        log_llm_api_exception(
            e, base_llm_response_processor.litellm_call_id,
            redacted=not is_alias_denial and (transformed_exception is not None or is_grok_alias),
        )
'''
    return replace_once(source, old, new)


def patch_utils(source):
    old = '''        ### ALERTING ###
        await self.update_request_status(litellm_call_id=request_data.get("litellm_call_id", ""), status="fail")
        if AlertType.llm_exceptions in self.alert_types and not _is_client_error_exception(original_exception):
'''
    new = '''        # The alias failure callback runs below, after alerting and proxy-only
        # logging. Keep those earlier surfaces closed without altering the raw
        # exception used for provider-billed accounting and callback dispatch.
        carrier = request_data.get("proxy_server_request")
        carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
        grok_alias_failure = request_data.get("model") == "project-grok-native" or (
            isinstance(carrier_body, dict) and carrier_body.get("model") == "project-grok-native"
        )
        log_exception = (
            HTTPException(status_code=502, detail="Project Grok provider failed")
            if grok_alias_failure else original_exception
        )

        ### ALERTING ###
        await self.update_request_status(litellm_call_id=request_data.get("litellm_call_id", ""), status="fail")
        if AlertType.llm_exceptions in self.alert_types and not _is_client_error_exception(original_exception):
'''
    source = replace_once(source, old, new)
    source = replace_once(source,
        '''                        verbose_proxy_logger.exception(
                            "[Non-Blocking] Error in async_post_call_failure_hook callback: %s", e
                        )
''',
        '''                        if grok_alias_failure:
                            verbose_proxy_logger.error(
                                "[Non-Blocking] Project Grok provider failed in failure hook"
                            )
                        else:
                            verbose_proxy_logger.exception(
                                "[Non-Blocking] Error in async_post_call_failure_hook callback: %s", e
                            )
''')
    source = replace_once(source,
        '''                verbose_proxy_logger.exception("[Non-Blocking] Error setting up post_call_failure_hook callback: %s", e)
''',
        '''                if grok_alias_failure:
                    verbose_proxy_logger.error(
                        "[Non-Blocking] Project Grok provider failed in failure hook setup"
                    )
                else:
                    verbose_proxy_logger.exception(
                        "[Non-Blocking] Error setting up post_call_failure_hook callback: %s", e
                    )
''')
    source = replace_once(source,
        '''            litellm_debug_info: Final[str | None] = getattr(original_exception, "litellm_debug_info", None)
            exception_str = str(original_exception)
''',
        '''            litellm_debug_info: Final[str | None] = getattr(log_exception, "litellm_debug_info", None)
            exception_str = str(log_exception)
''')
    source = replace_once(source,
        '''                original_exception=original_exception,
                error_type=error_type,
            )

        request_data.update(
''',
        '''                original_exception=log_exception,
                error_type=error_type,
            )

        request_data.update(
''')
    return source


def patch_common(source):
    source = replace_once(source,
        '''from litellm.integrations.custom_guardrail import CustomGuardrail\n''',
        '''from litellm.exceptions import CallerCredentialAuthenticationError\nfrom litellm.integrations.custom_guardrail import CustomGuardrail\n''')
    old = '''def log_llm_api_exception(e: Exception, litellm_call_id: str | None) -> None:
    if getattr(e, "status_code", None) == 499'''
    new = '''def log_llm_api_exception(e: Exception, litellm_call_id: str | None, *, redacted: bool = False) -> None:
    if redacted:
        # The active except frame still holds the provider exception. Logging
        # with exc_info here would print that raw traceback despite a fixed e.
        verbose_proxy_logger.error(
            "litellm.proxy.proxy_server._handle_llm_api_exception(): sanitized failure - litellm_call_id=%s - %s",
            litellm_call_id, e,
            extra=MappingProxyType({"litellm_call_id": litellm_call_id}),
        )
        return
    if getattr(e, "status_code", None) == 499'''
    source = replace_once(source, old, new)
    source = replace_once(source,
        '''        # Use transformed exception if callback returned one, otherwise use original
        if transformed_exception is not None:
            e = transformed_exception
        litellm_debug_info:''',
        '''        # Keep the callback's own bounded request denials at their original 401.
        carrier = self.data.get("proxy_server_request")
        carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
        is_grok_alias = self.data.get("model") == "project-grok-native" or (
            isinstance(carrier_body, dict) and carrier_body.get("model") == "project-grok-native"
        )
        is_alias_denial = (is_grok_alias and isinstance(e, CallerCredentialAuthenticationError)
            and getattr(e, "message", None) == "litellm.AuthenticationError: Project Grok request rejected")
        if transformed_exception is not None and not is_alias_denial:
            e = transformed_exception
        litellm_debug_info:''')
    old = '''        except Exception as e:
            verbose_proxy_logger.exception(
                "litellm.proxy.proxy_server.async_data_generator(): Exception occured - %s", e
            )
            transformed_exception: Final = await proxy_logging_obj.post_call_failure_hook(
'''
    new = '''        except Exception as e:
            transformed_exception: Final = await proxy_logging_obj.post_call_failure_hook(
'''
    source = replace_once(source, old, new)
    old = '''            if transformed_exception is not None:
                e = transformed_exception
            verbose_proxy_logger.debug(
'''
    new = '''            if transformed_exception is None:
                carrier = request_data.get("proxy_server_request")
                carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
                is_grok_alias = request_data.get("model") == "project-grok-native" or (
                    isinstance(carrier_body, dict) and carrier_body.get("model") == "project-grok-native"
                )
                if is_grok_alias:
                    transformed_exception = HTTPException(status_code=502, detail="Project Grok provider failed")
            if transformed_exception is not None:
                e = transformed_exception
                # The active exception frame contains the raw provider error.
                log_llm_api_exception(e, request_litellm_call_id(request_data), redacted=True)
            else:
                verbose_proxy_logger.exception(
                    "litellm.proxy.proxy_server.async_data_generator(): Exception occured - %s", e
                )
            verbose_proxy_logger.debug(
'''
    source = replace_once(source, old, new)
    source = replace_once(source,
        '''            if isinstance(e, HTTPException):
                raise e
            stream_error_status: Final = error_status_code(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
            proxy_exception: Final = ProxyException(
                message=redact_internal_details_from_client_message(getattr(e, "message", str(e))),
                type=openai_error_type(e, stream_error_status),
                param=openai_error_param(e),
                code=stream_error_status,
            )
''',
        '''            carrier = request_data.get("proxy_server_request")
            carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
            is_grok_alias = request_data.get("model") == "project-grok-native" or (
                isinstance(carrier_body, dict) and carrier_body.get("model") == "project-grok-native"
            )
            if isinstance(e, HTTPException) and transformed_exception is not None and is_grok_alias:
                # Headers have gone out. Emit the fixed error as an SSE frame.
                proxy_exception = proxy_exception_from_http_exception(e, {})
            else:
                if isinstance(e, HTTPException):
                    raise e from None
                stream_error_status: Final = error_status_code(e, status.HTTP_500_INTERNAL_SERVER_ERROR)
                proxy_exception = ProxyException(
                    message=redact_internal_details_from_client_message(getattr(e, "message", str(e))),
                    type=openai_error_type(e, stream_error_status),
                    param=openai_error_param(e),
                    code=stream_error_status,
                )
''')
    return source


def patch_router(source):
    # The Messages router logs provider exceptions at INFO before the proxy
    # callback runs. Keep the error object for routing/accounting, but close
    # this specific alias log surface.
    source = replace_once(source,
        '''                except Exception as fallback_error:
                    # If fallback also fails, log and re-raise original error
                    verbose_router_logger.error("Fallback also failed: %s", fallback_error)
''',
        '''                except Exception as fallback_error:
                    # The original error still drives fallback and accounting.
                    verbose_router_logger.error(
                        "Fallback also failed: %s",
                        "Project Grok provider failed" if initial_kwargs.get("model") == "project-grok-native"
                        else fallback_error,
                    )
''')
    source = replace_once(source,
        '''            verbose_router_logger.info(
                "ageneric_api_call_with_fallbacks(model=%s)\\x1b[31m Exception %s\\x1b[0m", model, e
            )
''',
        '''            verbose_router_logger.info(
                "ageneric_api_call_with_fallbacks(model=%s)\\x1b[31m Exception %s\\x1b[0m",
                model, "Project Grok provider failed" if model == "project-grok-native" else e,
            )
''')
    source = replace_once(source,
        '''        if verbose_router_logger.isEnabledFor(logging.DEBUG):
            verbose_router_logger.debug("Traceback%s", redact_string(traceback.format_exc()))
        original_exception: Final = e
''',
        '''        if verbose_router_logger.isEnabledFor(logging.DEBUG):
            verbose_router_logger.debug(
                "Traceback%s", "Project Grok provider failed" if model_group == "project-grok-native"
                else redact_string(traceback.format_exc())
            )
        original_exception: Final = e
''')
    source = replace_once(source,
        '''            fallback_failure_exception_str = truncate_fallback_error_detail(redact_string(str(new_exception)))
''',
        '''            fallback_failure_exception_str = (
                "Project Grok provider failed" if model_group == "project-grok-native"
                else truncate_fallback_error_detail(redact_string(str(new_exception)))
            )
''')
    source = replace_once(source,
        '''            verbose_router_logger.error("Anthropic messages streaming fallback also failed: %s", fallback_error)
''',
        '''            verbose_router_logger.error(
                "Anthropic messages streaming fallback also failed: %s",
                "Project Grok provider failed" if initial_kwargs.get("model") == "project-grok-native"
                else fallback_error,
            )
''')
    return source


def patch_anthropic_streaming_iterator(source):
    # This adapter logs before the proxy's mid-stream failure callback can
    # select its fixed error. The adapter's model is the resolved backend on
    # this route, so use the original request carrier retained by Logging.
    return replace_once(source,
        '''            verbose_logger.exception("Anthropic Adapter - mid-stream error: %s", e)
            logging_obj: Final = self.litellm_logging_obj
''',
        '''            logging_obj: Final = self.litellm_logging_obj
            params = getattr(logging_obj, "litellm_params", None)
            carrier = params.get("proxy_server_request") if isinstance(params, Mapping) else None
            body = carrier.get("body") if isinstance(carrier, Mapping) else None
            if isinstance(body, Mapping) and body.get("model") == "project-grok-native":
                verbose_logger.error("Anthropic Adapter - mid-stream error: Project Grok provider failed")
            else:
                verbose_logger.exception("Anthropic Adapter - mid-stream error: %s", e)
''')


PATCHERS = {
    "proxy/anthropic_endpoints/endpoints.py": patch_endpoint,
    "proxy/utils.py": patch_utils,
    "proxy/common_request_processing.py": patch_common,
    "router.py": patch_router,
    "llms/anthropic/pass_through/adapters/streaming_iterator.py": patch_anthropic_streaming_iterator,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    if args.source_root is None:
        import importlib.util
        spec = importlib.util.find_spec("litellm")
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError("LiteLLM source unavailable")
        root = Path(next(iter(spec.submodule_search_locations)))
    else:
        root = args.source_root
    # Validate every file before changing any, so a mixed or wrong base fails.
    contents = {}
    for relative, expected in EXPECTED.items():
        path = root / relative
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("unexpected LiteLLM source hash: " + relative)
        contents[relative] = content.decode("utf-8")
    patched = {relative: PATCHERS[relative](content) for relative, content in contents.items()}
    for relative, content in patched.items():
        compile(content, str(root / relative), "exec")
    for relative, content in patched.items():
        (root / relative).write_text(content)
    print("guarded LiteLLM patch applied to 5 source files")


if __name__ == "__main__":
    main()
