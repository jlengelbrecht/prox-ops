"""Security invariants for the dedicated browser-test identity and Paperclip grant."""

import copy
import pathlib
import textwrap
import types
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
AUTHENTIK = ROOT / "kubernetes/apps/security/authentik/app"
PAPERCLIP = ROOT / "kubernetes/apps/ai/paperclip/app"
# Frozen from the thirteen-application live inventory in LIVE-FLOW-APP-AUDIT.
LIVE_APP_SLUGS = frozenset({
    "book-stack", "glycemicgpt", "hermes", "hindsight", "light-rag",
    "lite-llm", "n8n", "obsidian", "open-webui", "paperclip",
    "toolhive", "toolhive-mcp-gateway", "wizarr",
})


class BlueprintLoader(yaml.SafeLoader):
    pass


def construct_tag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node)
    else:
        value = loader.construct_scalar(node)
    return {"tag": suffix, "value": value}


BlueprintLoader.add_multi_constructor("!", construct_tag)


def reference(tag, value):
    return {"tag": tag, "value": value}


def entry(entries, model, entry_id):
    found = [e for e in entries if e.get("model") == model and e.get("id") == entry_id]
    if len(found) != 1:
        raise AssertionError(f"expected one {model} entry with id {entry_id}, got {len(found)}")
    return found[0]


class Groups:
    def __init__(self, names):
        self.names = names

    def filter(self, name):
        return Groups({name} if name in self.names else set())

    def exists(self):
        return bool(self.names)


def candidate(username="ai-browser-test", active=True, admin=False,
              identity=True, paperclip=True, pk=1, malformed_groups=False,
              marker=True, malformed_marker=False):
    names = set()
    if identity:
        names.add("ai-browser-test-identity")
    if paperclip:
        names.add("ai-browser-test-paperclip")
    return types.SimpleNamespace(pk=pk, username=username, is_active=active,
                                 is_superuser=admin,
                                 groups=None if malformed_groups else Groups(names),
                                 attributes=None if malformed_marker else
                                 {"prox-ops/agent-browser-identity": "ABS-1"} if marker else {})


def anchored_expression(value):
    """Resolve the supported blueprint Format/KeyOf shape with a test account ID."""
    if not isinstance(value, dict) or value.get("tag") != "Format":
        raise AssertionError("security expression must use blueprint !Format")
    parts = value.get("value")
    if not isinstance(parts, list) or len(parts) != 2 or parts[1] != reference("KeyOf", "user-ai-browser-test"):
        raise AssertionError("security expression must reference the blueprint account")
    if parts[0].count("%s") != 1:
        raise AssertionError("security expression must contain one account-ID placeholder")
    return parts[0] % 1


def check_security(config, external_secret, helm, route, policy):
    """Return violated invariants; kept callable for negative-control tests."""
    problems = []

    def require(condition, message):
        if not condition:
            problems.append(message)

    apps = yaml.load(config["data"]["applications.yaml"], Loader=BlueprintLoader)["entries"]
    source = yaml.load(config["data"]["source-entra.yaml"], Loader=BlueprintLoader)["entries"]
    sources = yaml.load(config["data"]["sources.yaml"], Loader=BlueprintLoader)["entries"]

    def keyof_targets(node):
        if isinstance(node, dict):
            if node.get("tag") == "KeyOf":
                yield node["value"]
            else:
                for value in node.values():
                    yield from keyof_targets(value)
        elif isinstance(node, list):
            for value in node:
                yield from keyof_targets(value)

    seen_ids = set()
    for app_entry in apps:
        for target in keyof_targets(app_entry):
            require(target in seen_ids, f"!KeyOf {target} must reference an earlier applications.yaml entry")
        if app_entry.get("id"):
            seen_ids.add(app_entry["id"])

    user = entry(apps, "authentik_core.user", "user-ai-browser-test")
    identity = entry(apps, "authentik_core.group", "group-ai-browser-test-identity")
    group = entry(apps, "authentik_core.group", "group-ai-browser-test-paperclip")
    flow = entry(apps, "authentik_flows.flow", "flow-ai-browser-test")
    identify = entry(apps, "authentik_stages_identification.identificationstage", "stage-ai-browser-test-identification")
    deny = entry(apps, "authentik_stages_deny.denystage", "stage-ai-browser-test-deny")
    deny_policy = entry(apps, "authentik_policies_expression.expressionpolicy", "policy-ai-browser-test-deny-unapproved")
    password = entry(apps, "authentik_stages_password.passwordstage", "stage-ai-browser-test-password")
    totp = entry(apps, "authentik_stages_authenticator_validate.authenticatorvalidatestage", "stage-ai-browser-test-totp")
    source_guard = entry(apps, "authentik_policies_expression.expressionpolicy", "policy-ai-browser-test-source-login")
    exclusion = entry(apps, "authentik_policies_expression.expressionpolicy", "policy-deny-ungranted-ai-browser-test")
    login = entry(apps, "authentik_stages_user_login.userloginstage", "stage-ai-browser-test-login")
    paperclip = entry(apps, "authentik_core.application", "app-paperclip")
    admin_grant = entry(apps, "authentik_policies.policybinding", "binding-paperclip-admins")
    agent_grant = entry(apps, "authentik_policies.policybinding", "binding-paperclip-ai-browser-test")

    require(user.get("state") == "created" and user.get("identifiers") == {"path": "agents/browser-test/ai-browser-test"}, "account must be create-only and retain its lookup across a username rename")
    ua = user.get("attrs", {})
    require(ua.get("username") == "ai-browser-test" and ua.get("type") == "internal" and ua.get("is_active") is True, "account must be an active internal user")
    require(ua.get("password") == reference("Env", "AUTHENTIK_AI_BROWSER_TEST_INITIAL_PASSWORD"), "initial password must be read from the secret-backed environment")
    require(not ua.get("is_superuser") and not ua.get("roles") and ua.get("groups") == [], "account must start without admin or broad groups")
    require(ua.get("attributes", {}).get("goauthentik.io/user/can-change-username") is False, "agent must not be able to rename its own bootstrap username")
    require(ua.get("attributes", {}).get("goauthentik.io/user/can-change-email") is False, "agent must not edit its email for UID linking")
    require(ua.get("attributes", {}).get("prox-ops/agent-browser-identity") == "ABS-1", "account must retain its independent administrator-controlled marker")
    ia = identity.get("attrs", {})
    require(identity.get("state") == "created" and identity.get("identifiers") == {"name": "ai-browser-test-identity"}, "identity group must be create-only and independent of username updates")
    require(ia.get("is_superuser") is False and ia.get("parents") == [] and ia.get("roles") == [], "identity group must not grant administrator permissions")
    require(ia.get("users") == [reference("KeyOf", "user-ai-browser-test")], "identity group must initially contain only the dedicated account")
    ga = group.get("attrs", {})
    require(ga.get("is_superuser") is False and ga.get("parents") == [] and ga.get("roles") == [], "agent group must not inherit administrator permissions")
    require(ga.get("users") == [reference("KeyOf", "user-ai-browser-test")], "agent group must contain only the dedicated account")
    require(apps.index(user) < apps.index(exclusion), "account must exist before its database ID is formatted into a policy")
    require(flow.get("attrs", {}).get("authentication") == "require_unauthenticated", "agent flow must start from a fresh unauthenticated context")
    require(identify.get("attrs", {}).get("user_fields") == ["username"] and identify.get("attrs", {}).get("sources") == [], "agent identification must use only a local username")
    require(identify.get("attrs", {}).get("pretend_user_exists") is True, "agent flow must not reveal account existence")
    require(not identify.get("attrs", {}).get("password_stage") and not identify.get("attrs", {}).get("webauthn_stage"), "identification must not replace or bypass the dedicated password stage")
    require(deny.get("identifiers", {}).get("name") == "ai-browser-test-deny", "deny stage is required")

    bindings = {e["identifiers"]["order"]: e for e in apps if e.get("model") == "authentik_flows.flowstagebinding" and e.get("identifiers", {}).get("target") == reference("KeyOf", "flow-ai-browser-test")}
    require(set(bindings) == {10, 15, 20, 30, 100}, "flow must have identification, deny, password, TOTP and login stages in order")
    for order, stage_id in [(10, "stage-ai-browser-test-identification"), (15, "stage-ai-browser-test-deny"), (20, "stage-ai-browser-test-password"), (30, "stage-ai-browser-test-totp"), (100, "stage-ai-browser-test-login")]:
        require(bindings.get(order, {}).get("identifiers", {}).get("stage") == reference("KeyOf", stage_id), f"wrong stage at order {order}")
    require(bindings.get(15, {}).get("attrs") == {"evaluate_on_plan": False, "re_evaluate_policies": True}, "deny policy must run after identification")
    guard = [e for e in apps if e.get("model") == "authentik_policies.policybinding" and e.get("identifiers", {}).get("target") == reference("KeyOf", "binding-ai-browser-test-deny")]
    require(len(guard) == 1 and guard[0].get("attrs", {}).get("policy") == reference("KeyOf", "policy-ai-browser-test-deny-unapproved") and guard[0].get("attrs", {}).get("failure_result") is True, "deny policy must fail closed")
    expression = deny_policy.get("attrs", {}).get("expression", "")
    for term in ['pending_user', 'user.username == "ai-browser-test"', 'user.is_active', 'not user.is_superuser', 'user.groups.filter(name="ai-browser-test-identity").exists()']:
        require(term in expression, f"deny policy missing restriction: {term}")
    # Exercise the policy against representative Authentik pending-user states.
    # True runs the Deny stage; only the approved account may return False.
    try:
        namespace = {}
        exec("def evaluate(request):\n" + textwrap.indent(expression, "    "), namespace)
        for user_case, expected in [
            (candidate(), False), (candidate(username="other-user"), True),
            (candidate(active=False), True), (candidate(admin=True), True),
            (candidate(identity=False), True), (candidate(paperclip=False), False),
            (None, True),
        ]:
            request = types.SimpleNamespace(context={"flow_plan": types.SimpleNamespace(context={"pending_user": user_case})})
            require(namespace["evaluate"](request) is expected, "deny policy admits an unapproved identity or blocks the approved one")
    except Exception as exc:
        problems.append(f"deny policy failed to evaluate locally: {type(exc).__name__}")
    require(password.get("attrs", {}).get("backends") == ["authentik.core.auth.InbuiltBackend"], "password stage must use only local password backend")
    ta = totp.get("attrs", {})
    require(ta.get("not_configured_action") == "deny" and ta.get("device_classes") == ["totp"] and ta.get("configuration_stages") == [] and ta.get("last_auth_threshold") == "seconds=0", "TOTP must be required on every fresh login without self-enrollment")
    # The default/Brand flow can be reached through a provider. Guard its
    # live User Login stage, leaving the human password/passkey stages intact.
    default_target = reference("Find", ["authentik_flows.flow", ["slug", "default-authentication-flow"]])
    require(not any(e.get("model") == "authentik_flows.flowstagebinding" and e.get("identifiers", {}).get("target") == default_target and e.get("identifiers", {}).get("stage") == reference("KeyOf", "stage-ai-browser-test-totp") for e in apps), "dedicated TOTP stage must not be wired into default login")
    # In 2026.5.3 the source-auth flow logs in at order 0; enrollment logs in
    # at order 2 after User Write. All alternate paths guard their live login.
    for flow_slug, stage_name, order, binding_id in [
        ("default-authentication-flow", "default-authentication-login", 100, "binding-ai-browser-test-default-login"),
        ("default-source-authentication", "default-source-authentication-login", 0, "binding-ai-browser-test-source-auth-login"),
        ("default-source-enrollment", "default-source-enrollment-login", 2, "binding-ai-browser-test-source-enroll-login"),
        ("bookstack-authentication", "bookstack-user-login", 10, "binding-ai-browser-test-bookstack-login"),
        ("glycemicgpt-authentication-flow", "default-authentication-login", 30, "binding-ai-browser-test-glycemicgpt-login"),
        ("initial-setup", "default-authentication-login", 100, "binding-ai-browser-test-initial-setup-login"),
    ]:
        binding = entry(apps, "authentik_flows.flowstagebinding", binding_id)
        require(binding.get("identifiers") == {"target": reference("Find", ["authentik_flows.flow", ["slug", flow_slug]]), "stage": reference("Find", ["authentik_stages_user_login.userloginstage", ["name", stage_name]]), "order": order}, f"{flow_slug} login binding must be guarded")
        require(binding.get("attrs") == {"evaluate_on_plan": False, "re_evaluate_policies": True, "policy_engine_mode": "all"}, f"{flow_slug} guard must run immediately before login")
        guards = [e for e in apps if e.get("model") == "authentik_policies.policybinding" and e.get("identifiers", {}).get("target") == reference("KeyOf", binding_id)]
        require(len(guards) == 1 and guards[0].get("attrs") == {"policy": reference("KeyOf", "policy-ai-browser-test-source-login"), "failure_result": False}, f"{flow_slug} login must fail closed")
    for source_entry in [*sources, *source]:
        if source_entry.get("model") in {"authentik_sources_oauth.oauthsource", "authentik_sources_plex.plexsource"}:
            require(source_entry.get("attrs", {}).get("authentication_flow") == reference("Context", "authentication_flow") and source_entry.get("attrs", {}).get("enrollment_flow") == reference("Context", "enrollment_flow"), "checked-in source bypasses guarded flows")
    for key in ("sources.yaml", "source-entra.yaml"):
        context = yaml.load(config["data"][key], Loader=BlueprintLoader)["context"]
        for kind in ("authentication", "enrollment"):
            require(context.get(f"{kind}_flow") == reference("Find", ["authentik_flows.flow", ["slug", f"default-source-{kind}"]]), "source flow context bypasses guarded login")
    require(entry(sources, "authentik_sources_oauth.oauthsource", "source-uid")["attrs"]["user_matching_mode"] == "email_link", "UID linking path must stay covered")
    try:
        namespace = {}
        exec("def evaluate(request):\n" + textwrap.indent(anchored_expression(source_guard["attrs"]["expression"]), "    "), namespace)
        for user_case, expected in [(candidate(), False), (candidate(paperclip=False), False), (candidate(username="renamed", identity=False, paperclip=False, marker=False), False), (candidate(username="renamed", identity=False, paperclip=False, malformed_groups=True, malformed_marker=True), False), (candidate(username="renamed", identity=False, paperclip=False, pk=2), False), (candidate(username="human", identity=False, paperclip=False, pk=2, marker=False), True), (None, False)]:
            request = types.SimpleNamespace(context={"flow_plan": types.SimpleNamespace(context={"pending_user": user_case})})
            require(namespace["evaluate"](request) is expected, "alternate login admits the agent or blocks an ordinary human")
        require(namespace["evaluate"](types.SimpleNamespace(context={})) is False, "missing flow context must deny alternate login")
    except Exception as exc:
        problems.append(f"alternate login guard failed to evaluate locally: {type(exc).__name__}")
    la = login.get("attrs", {})
    require(la.get("session_duration") == "seconds=0" and la.get("remember_me_offset") == "seconds=0" and la.get("remember_device") == "seconds=0" and la.get("terminate_other_sessions") is True, "agent session must be browser-scoped without remembered-device bypass")

    require(paperclip.get("attrs", {}).get("policy_engine_mode") == "any", "Paperclip grants must be ORed")
    require(admin_grant.get("attrs", {}).get("group") == reference("Find", ["authentik_core.group", ["name", "authentik Admins"]]), "existing owner Paperclip grant must remain")
    require(agent_grant.get("attrs", {}).get("group") == reference("KeyOf", "group-ai-browser-test-paperclip") and agent_grant.get("attrs", {}).get("enabled") is True, "Paperclip requires explicit agent group binding")
    require(agent_grant.get("identifiers", {}).get("order") != admin_grant.get("identifiers", {}).get("order"), "Paperclip grants require distinct orders")
    # No binding means allow everyone; with `any`, a permissive binding can
    # override a deny. All ungranted apps therefore need the exclusion and
    # `all`, including apps that already have an admin-only binding.
    active_apps = {e["identifiers"]["slug"]: e for e in apps if e.get("model") == "authentik_core.application" and e.get("state") != "absent"}
    require(LIVE_APP_SLUGS.issubset(active_apps), "live application inventory has an unblueprinted app")
    light_rag = active_apps.get("light-rag", {})
    require(light_rag.get("conditions") == [reference("Find", ["authentik_core.application", ["slug", "light-rag"]])], "LightRAG must update only the existing live application")
    try:
        namespace = {}
        expression = anchored_expression(exclusion.get("attrs", {}).get("expression"))
        exec("def evaluate(request):\n" + textwrap.indent(expression, "    "), namespace)
        for user_case, expected in [
            (candidate(), False),
            (candidate(username="renamed-agent", identity=False, paperclip=False, marker=False), False),
            (candidate(username="renamed-agent", identity=False, paperclip=False, malformed_groups=True, malformed_marker=True), False),
            (candidate(username="renamed-agent", identity=False, paperclip=False, pk=2, malformed_groups=True), False),
            (candidate(paperclip=False), False),
            (candidate(username="human", identity=False, paperclip=False, pk=2, marker=False), True),
            (candidate(username="admin", identity=False, paperclip=False, pk=2, marker=False), True),
        ]:
            require(namespace["evaluate"](types.SimpleNamespace(user=user_case)) is expected, "application exclusion changes agent or human access")
    except Exception as exc:
        problems.append(f"application exclusion failed to evaluate locally: {type(exc).__name__}")
    for slug, app in active_apps.items():
        app_bindings = [e for e in apps if e.get("model") == "authentik_policies.policybinding" and e.get("identifiers", {}).get("target") == reference("Find", ["authentik_core.application", ["slug", slug]])]
        exclusions = [e for e in app_bindings if e.get("attrs", {}).get("policy") == reference("KeyOf", "policy-deny-ungranted-ai-browser-test") and e.get("attrs", {}).get("enabled") is True]
        if slug == "paperclip":
            require(not exclusions, "Paperclip must use its explicit group grants")
        else:
            require(app.get("attrs", {}).get("policy_engine_mode") == "all", f"{slug} must require every binding to pass")
            require(len(exclusions) == 1, f"{slug} must exclude the agent even with another allow binding")
            if slug == "light-rag" and exclusions:
                require(exclusions[0].get("conditions") == [reference("Find", ["authentik_core.application", ["slug", "light-rag"]])], "LightRAG binding must skip when the application is absent")
    for e in apps:
        if e.get("model") == "authentik_policies.policybinding" and e.get("id") != "binding-paperclip-ai-browser-test":
            require(e.get("attrs", {}).get("group") != reference("KeyOf", "group-ai-browser-test-paperclip"), "agent group must not reach another app")
        if e.get("model") == "authentik_policies.policybinding":
            require(e.get("attrs", {}).get("group") != reference("KeyOf", "group-ai-browser-test-identity"), "identity classification group must never grant application access")
    require(all(e.get("model") != "authentik_core.application" or e.get("id") != "app-admin" for e in apps), "no agent admin application")

    human_stages = [e for e in source if e.get("model") == "authentik_stages_identification.identificationstage"]
    require({e.get("identifiers", {}).get("name") for e in human_stages} == {"default-authentication-identification", "external-authentication-identification"}, "human identification stages must remain")
    for stage in human_stages:
        sources = stage.get("attrs", {}).get("sources", [])
        require(reference("Find", ["authentik_core.source", ["slug", "unifi-identity-enterprise"]]) in sources and reference("KeyOf", "source-entra") in sources, "Entra and UID choices must remain")

    data = {e.get("secretKey"): e.get("remoteRef") for e in external_secret["spec"]["data"]}
    require(data.get("AUTHENTIK_AI_BROWSER_TEST_INITIAL_PASSWORD") == {"key": "authentik", "property": "ai-browser-test-initial-password"}, "initial password must come from existing Automation vault item")
    env = {e.get("name"): e.get("valueFrom", {}).get("secretKeyRef") for e in helm["spec"]["values"]["global"]["env"]}
    require(env.get("AUTHENTIK_AI_BROWSER_TEST_INITIAL_PASSWORD") == {"name": "authentik-secret", "key": "AUTHENTIK_AI_BROWSER_TEST_INITIAL_PASSWORD"}, "password environment must reference Kubernetes Secret")
    require(policy["spec"]["targetRefs"] == [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": "paperclip"}], "forward auth must target only Paperclip UI route")
    routes = {d["metadata"]["name"]: d for d in yaml.safe_load_all(route) if isinstance(d, dict) and d.get("kind") == "HTTPRoute"}
    require({"paperclip", "paperclip-api", "paperclip-outpost"}.issubset(routes), "Paperclip UI, API and outpost routes must remain separate")
    return problems


class AgentBrowserSSOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load((AUTHENTIK / "configmap-blueprints.yaml").read_text())
        cls.external_secret = yaml.safe_load((AUTHENTIK / "externalsecret.yaml").read_text())
        cls.helm = yaml.safe_load((AUTHENTIK / "helmrelease.yaml").read_text())
        cls.route = (PAPERCLIP / "httproute.yaml").read_text()
        cls.policy = yaml.safe_load((PAPERCLIP / "securitypolicy.yaml").read_text())

    def check(self, config=None, external_secret=None, helm=None):
        return check_security(config or self.config, external_secret or self.external_secret, helm or self.helm, self.route, self.policy)

    def assert_mutations_detected(self, cases, key="applications.yaml"):
        for old, new in cases:
            with self.subTest(change=old[:60]):
                mutated = copy.deepcopy(self.config)
                self.assertIn(old, mutated["data"][key])
                mutated["data"][key] = mutated["data"][key].replace(old, new, 1)
                self.assertTrue(self.check(config=mutated))

    def test_security_contract(self):
        self.assertEqual([], self.check())

    def test_forward_keyof_reference_is_rejected(self):
        mutated = copy.deepcopy(self.config)
        blueprint = yaml.load(mutated["data"]["applications.yaml"], Loader=BlueprintLoader)
        entries = blueprint["entries"]
        source_guard = entry(entries, "authentik_policies_expression.expressionpolicy", "policy-ai-browser-test-source-login")
        default_binding = next(e for e in entries if e.get("model") == "authentik_policies.policybinding"
                               and e.get("identifiers", {}).get("target") == reference("KeyOf", "binding-ai-browser-test-default-login"))
        entries.remove(source_guard)
        entries.insert(entries.index(default_binding) + 1, source_guard)
        mutated["data"]["applications.yaml"] = yaml.safe_dump(blueprint)
        self.assertIn("!KeyOf policy-ai-browser-test-source-login must reference an earlier applications.yaml entry",
                      self.check(config=mutated))

    def test_missing_factor_or_restriction_is_rejected(self):
        self.assert_mutations_detected([
            ("not_configured_action: deny", "not_configured_action: skip"),
            ("device_classes: [totp]", "device_classes: [webauthn]"),
            ("last_auth_threshold: seconds=0", "last_auth_threshold: days=30"),
            ('user.username == "ai-browser-test"', "True"),
            ("failure_result: true", "failure_result: false"),
            ("id: group-ai-browser-test-identity\n    state: created", "id: group-ai-browser-test-identity\n    state: present"),
            ("users:\n        - !KeyOf user-ai-browser-test", "users: []"),
            ("goauthentik.io/user/can-change-username: false", "goauthentik.io/user/can-change-username: true"),
            ("goauthentik.io/user/can-change-email: false", "goauthentik.io/user/can-change-email: true"),
            ("prox-ops/agent-browser-identity: ABS-1", "prox-ops/agent-browser-identity: other"),
            ("path: agents/browser-test/ai-browser-test", "username: ai-browser-test"),
        ])

    def test_admin_grant_or_missing_paperclip_grant_is_rejected(self):
        self.assert_mutations_detected([
            ("is_superuser: false", "is_superuser: true"),
            ("group: !KeyOf group-ai-browser-test-paperclip", 'group: !Find [authentik_core.group, [name, "authentik Admins"]]'),
        ])

    def test_direct_password_and_totp_bypass_is_rejected(self):
        self.assert_mutations_detected([
            ("backends:\n        - authentik.core.auth.InbuiltBackend", "backends:\n        - authentik.core.auth.TokenBackend"),
            ("target: !KeyOf flow-ai-browser-test, stage: !KeyOf stage-ai-browser-test-password, order: 20", "target: !KeyOf flow-ai-browser-test, stage: !KeyOf stage-ai-browser-test-deny, order: 20"),
            ("target: !KeyOf flow-ai-browser-test, stage: !KeyOf stage-ai-browser-test-totp, order: 30", "target: !KeyOf flow-ai-browser-test, stage: !KeyOf stage-ai-browser-test-totp, order: 110"),
            ("user_fields: [username]\n      sources: []", "user_fields: [username]\n      sources: [!Find [authentik_core.source, [slug, entra]]]"),
        ])

    def test_ungranted_application_bypass_is_rejected(self):
        self.assert_mutations_detected([
            ('return not (\n              user.pk == %s', 'return not (\n              False'),
            ("name: Open WebUI\n      slug: open-webui\n      group: AI\n      policy_engine_mode: all", "name: Open WebUI\n      slug: open-webui\n      group: AI\n      policy_engine_mode: any"),
            ("target: !Find [authentik_core.application, [slug, open-webui]], order: 90", "target: !Find [authentik_core.application, [slug, paperclip]], order: 90"),
            ("name: Hermes\n      slug: hermes\n      group: AI\n      policy_engine_mode: all", "name: Hermes\n      slug: hermes\n      group: AI\n      policy_engine_mode: any"),
        ])

    def test_identity_group_cannot_become_an_application_grant(self):
        original = "group: !KeyOf group-ai-browser-test-paperclip"
        mutated = copy.deepcopy(self.config)
        self.assertIn(original, mutated["data"]["applications.yaml"])
        mutated["data"]["applications.yaml"] = mutated["data"]["applications.yaml"].replace(
            original, "group: !KeyOf group-ai-browser-test-identity", 1
        )
        self.assertTrue(self.check(config=mutated))

    def test_account_marker_fallback_is_required_in_all_session_policies(self):
        text = self.config["data"]["applications.yaml"]
        for clause, replacement in [
            ('or user.attributes.get("prox-ops/agent-browser-identity") == "ABS-1"', "or False"),
            ('and user.attributes.get("prox-ops/agent-browser-identity") != "ABS-1"', "and True"),
        ]:
            with self.subTest(policy=clause):
                self.assertEqual(text.count(clause), 1)
                mutated = copy.deepcopy(self.config)
                mutated["data"]["applications.yaml"] = text.replace(clause, replacement, 1)
                self.assertTrue(self.check(config=mutated))

    def test_source_login_bypass_is_rejected(self):
        for old, new in [
            ("user.pk != %s", "user.pk != 2"),
            ("id: binding-ai-browser-test-default-login\n    identifiers: {target: !Find [authentik_flows.flow, [slug, default-authentication-flow]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-authentication-login]], order: 100}\n    attrs: {evaluate_on_plan: false, re_evaluate_policies: true, policy_engine_mode: all}", "id: binding-ai-browser-test-default-login\n    identifiers: {target: !Find [authentik_flows.flow, [slug, default-authentication-flow]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-authentication-login]], order: 100}\n    attrs: {evaluate_on_plan: false, re_evaluate_policies: true, policy_engine_mode: any}"),
            ("id: binding-ai-browser-test-source-auth-login\n    identifiers: {target: !Find [authentik_flows.flow, [slug, default-source-authentication]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-source-authentication-login]], order: 0}\n    attrs: {evaluate_on_plan: false, re_evaluate_policies: true, policy_engine_mode: all}", "id: binding-ai-browser-test-source-auth-login\n    identifiers: {target: !Find [authentik_flows.flow, [slug, default-source-authentication]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-source-authentication-login]], order: 0}\n    attrs: {evaluate_on_plan: false, re_evaluate_policies: true, policy_engine_mode: any}"),
            ("target: !KeyOf binding-ai-browser-test-source-enroll-login", "target: !KeyOf binding-ai-browser-test-source-auth-login"),
            ("user_matching_mode: email_link", "user_matching_mode: identifier"),
        ]:
            with self.subTest(change=old):
                mutated = copy.deepcopy(self.config)
                key = "sources.yaml" if old == "user_matching_mode: email_link" else "applications.yaml"
                self.assertIn(old, mutated["data"][key])
                mutated["data"][key] = mutated["data"][key].replace(old, new, 1)
                self.assertTrue(self.check(config=mutated))

    def test_live_light_rag_and_custom_login_mutations_are_rejected(self):
        self.assert_mutations_detected([
            ("identifiers: {slug: light-rag}", "identifiers: {slug: light-rag-copy}"),
            ("identifiers: {slug: light-rag}\n    attrs:\n      policy_engine_mode: all", "identifiers: {slug: light-rag}\n    attrs:\n      policy_engine_mode: any"),
            ("target: !Find [authentik_core.application, [slug, light-rag]], order: 90", "target: !Find [authentik_core.application, [slug, paperclip]], order: 90"),
            ("[slug, bookstack-authentication]], stage: !Find [authentik_stages_user_login.userloginstage, [name, bookstack-user-login]], order: 10", "[slug, bookstack-authentication]], stage: !Find [authentik_stages_user_login.userloginstage, [name, bookstack-user-login]], order: 11"),
            ("[slug, glycemicgpt-authentication-flow]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-authentication-login]], order: 30", "[slug, glycemicgpt-authentication-flow]], stage: !Find [authentik_stages_user_login.userloginstage, [name, default-authentication-login]], order: 31"),
            ("target: !KeyOf binding-ai-browser-test-glycemicgpt-login, order: 0", "target: !KeyOf binding-ai-browser-test-bookstack-login, order: 0"),
            ("target: !KeyOf binding-ai-browser-test-initial-setup-login, order: 0", "target: !KeyOf binding-ai-browser-test-bookstack-login, order: 0"),
        ])

if __name__ == "__main__":
    unittest.main()
