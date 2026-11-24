"""Safety and exposure contract for the netboot.xyz PXE server manifests."""

import pathlib
import re
import struct
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/network/netbootxyz'
IMAGE = r'^ghcr\.io/netbootxyz/netbootxyz:\d+\.\d+\.\d+-nbxyz\d+@sha256:[0-9a-f]{64}$'
SCHEMATIC = '536b92328799fed89ad41d8ecb85ed3de23ab99ba7eded5ab6b6ef1197dc6b17'
CAPS = {'CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'KILL', 'NET_BIND_SERVICE', 'SETGID', 'SETUID'}


def documents():
    docs = []
    for name in yaml.safe_load((APP / 'app/kustomization.yaml').read_text())['resources']:
        docs += [d for d in yaml.safe_load_all((APP / 'app' / name).read_text()) if d]
    return docs


GATEWAY = {'io.kubernetes.pod.namespace': 'network',
           'gateway.envoyproxy.io/owning-gateway-name': 'envoy-internal'}
BOOT, EDITOR = {('69', 'UDP'), ('80', 'TCP')}, {('3000', 'TCP')}
GATEWAY_EDITOR_RULE = {'fromEndpoints': [{'matchLabels': GATEWAY}],
                       'toPorts': [{'ports': [{'port': '3000', 'protocol': 'TCP'}]}]}


def policy_problems(policy, editor_routed=False):
    """Boot ports open to LAN and cluster. The editor port 3000 admits nothing
    (port-forward only) unless editor_routed, and then only envoy-internal."""
    problems, boot_open, routed = [], False, False
    for rule in policy.get('ingress', []):
        # L7 rules, TLS or SNI on a toPorts entry filter traffic; flattening would hide them.
        problems += [f'toPorts entry with more than ports: {to}'
                     for to in rule.get('toPorts', []) if set(to) - {'ports'}]
        entries = [p for to in rule.get('toPorts', []) for p in to.get('ports', [])]
        # A range (endPort) or any other key can open more than (port, protocol) says.
        problems += [f'port entry other than port/protocol: {p}'
                     for p in entries if set(p) - {'port', 'protocol'}]
        ports = {(p['port'], p.get('protocol', 'ANY')) for p in entries}
        sources = {k: v for k, v in rule.items() if k != 'toPorts'}
        if not ports:
            problems.append(f'ingress without a port restriction from {sources}')
        elif ports <= BOOT:
            boot_open |= ports == BOOT and set(sources.get('fromEntities', [])) >= {'world', 'cluster'}
        elif ports == EDITOR and sources == {'fromEndpoints': [{'matchLabels': GATEWAY}]}:
            routed = True
        else:
            problems.append(f'unexpected ingress to {sorted(ports)} from {sources}')
    if not boot_open:
        problems.append('PXE clients must reach TFTP 69 and HTTP 80 without login')
    if routed != editor_routed:
        problems.append('the internal gateway must reach the editor on 3000' if editor_routed
                        else 'the editor on 3000 must stay closed: port-forward only')
    return problems


def topf_talos_version():
    return re.search(r'^talosVersion:\s*v(\S+)$', (ROOT / 'talos/topf.yaml').read_text(),
                     re.M).group(1)


class ManifestContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = documents()
        cls.by_kind = {}
        for doc in cls.docs:
            cls.by_kind.setdefault(doc['kind'], []).append(doc)
        cls.deployment = cls.by_kind['Deployment'][0]
        cls.pod = cls.deployment['spec']['template']['spec']
        cls.container = cls.pod['containers'][0]
        cls.env = {e['name']: e['value'] for e in cls.container['env']}
        cls.data = cls.by_kind['ConfigMap'][0]['data']

    def test_flux_leaves_ipxe_variables_alone(self):
        # flate renders the enrolment; this checks iPXE ${...} reaches the pod unchanged.
        self.assertNotIn('postBuild', yaml.safe_load((APP / 'ks.yaml').read_text())['spec'])
        self.assertEqual(self.by_kind['ConfigMap'][0]['metadata']['annotations']
                         ['kustomize.toolkit.fluxcd.io/substitute'], 'disabled')

    def test_only_expected_kinds_and_no_secrets(self):
        self.assertEqual(sorted(self.by_kind), ['CiliumNetworkPolicy', 'ConfigMap', 'Deployment',
                                                'PersistentVolumeClaim', 'Service'])
        text = '\n'.join(p.read_text() for p in APP.rglob('*.yaml'))
        for marker in ('talos.config', 'PRIVATE KEY', 'secretKeyRef', 'secretRef', 'machine:'):
            self.assertNotIn(marker, text)
        self.assertNotRegex(text, r'(?i)\b(password|passwd|token|secret)\b\s*[:=]')

    def test_no_public_exposure(self):
        admin, lb = sorted(self.by_kind['Service'], key=lambda s: s['spec']['type'])
        self.assertEqual(lb['spec']['type'], 'LoadBalancer')
        self.assertEqual(lb['spec']['externalTrafficPolicy'], 'Cluster')
        self.assertEqual(lb['metadata']['annotations']['lbipam.cilium.io/ips'], '10.20.67.28')
        self.assertNotIn('network', lb['metadata'].get('labels', {}))
        self.assertEqual({(p['port'], p['protocol']) for p in lb['spec']['ports']},
                         {(69, 'UDP'), (8080, 'TCP')})
        self.assertEqual(admin['spec']['type'], 'ClusterIP')
        self.assertEqual([p['port'] for p in admin['spec']['ports']], [3000])

    def test_policy_admits_boot_ports_openly_and_never_the_editor(self):
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        self.assertEqual(policy['endpointSelector']['matchLabels'],
                         self.deployment['spec']['selector']['matchLabels'])
        # No egress rules, so DNS and release downloads are not default-denied.
        self.assertEqual(set(policy), {'endpointSelector', 'ingress'})
        self.assertEqual(policy_problems(policy), [])

    def test_policy_rejects_editor_exposure(self):
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        editor = {'port': '3000', 'protocol': 'TCP'}
        for name, rule in [
            ('world', {'fromEntities': ['world'], 'toPorts': [{'ports': [editor]}]}),
            ('cluster', {'fromEntities': ['cluster'], 'toPorts': [{'ports': [editor]}]}),
            ('any pod', {'fromEndpoints': [{}], 'toPorts': [{'ports': [editor]}]}),
            ('external gateway', {'fromEndpoints': [{'matchLabels': {
                **GATEWAY, 'gateway.envoyproxy.io/owning-gateway-name': 'envoy-external'}}],
                'toPorts': [{'ports': [editor]}]}),
            ('all ports', {'fromEndpoints': [{'matchLabels': GATEWAY}]}),
            ('cidr', {'fromCIDR': ['10.20.65.0/24'], 'toPorts': [{'ports': [editor]}]}),
            ('range', {'fromEntities': ['world'], 'toPorts': [{'ports': [
                {'port': '80', 'protocol': 'TCP', 'endPort': 3000}]}]}),
            ('any protocol', {'fromEndpoints': [{'matchLabels': GATEWAY}],
                              'toPorts': [{'ports': [{'port': '3000'}]}]}),
            # Correctly scoped, but nothing routes to the editor yet.
            ('internal gateway', GATEWAY_EDITOR_RULE),
        ]:
            with self.subTest(rule=name):
                mutated = {**policy, 'ingress': policy['ingress'] + [rule]}
                self.assertTrue(policy_problems(mutated))

    def test_policy_check_controls(self):
        """The checker accepts only the scoped gateway rule when routing is expected."""
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        routed = {**policy, 'ingress': policy['ingress'] + [GATEWAY_EDITOR_RULE]}
        self.assertEqual(policy_problems(routed, editor_routed=True), [])
        self.assertTrue(policy_problems(policy, editor_routed=True))  # rule missing
        self.assertTrue(policy_problems({**policy, 'ingress': []}))  # PXE closed
        self.assertTrue(policy_problems({**policy, 'ingress': [  # cluster only, no LAN
            {**policy['ingress'][0], 'fromEntities': ['cluster']}]}))
        # Boot ports split across entries, with an HTTP path rule on 80: menus would be denied.
        self.assertTrue(policy_problems({**policy, 'ingress': [{
            'fromEntities': ['world', 'cluster'],
            'toPorts': [{'ports': [{'port': '69', 'protocol': 'UDP'}]},
                        {'ports': [{'port': '80', 'protocol': 'TCP'}],
                         'rules': {'http': [{'path': '/health'}]}}]}]}))
        # The same split without restrictions is still an open boot rule.
        self.assertEqual(policy_problems({**policy, 'ingress': [{
            'fromEntities': ['world', 'cluster'],
            'toPorts': [{'ports': [{'port': '69', 'protocol': 'UDP'}]},
                        {'ports': [{'port': '80', 'protocol': 'TCP'}]}]}]}), [])

    def test_pinned_versions_and_single_port_tftp(self):
        # Pin shapes, not values, so Renovate bumps pass.
        self.assertRegex(self.container['image'], IMAGE)
        self.assertRegex(self.env['MENU_VERSION'], r'^\d+\.\d+\.\d+$')
        self.assertRegex(self.env['IPXE_SB_VERSION'], r'^v\d+\.\d+\.\d+$')
        self.assertEqual(self.env['TFTPD_OPTS'], '--tftp-single-port')

    def test_pod_has_no_host_access_and_minimal_capabilities(self):
        for key in ('hostNetwork', 'hostPID', 'hostIPC'):
            self.assertFalse(self.pod.get(key, False))
        for volume in self.pod['volumes']:
            self.assertNotIn('hostPath', volume)
        for port in self.container['ports']:
            self.assertNotIn('hostPort', port)
        context = self.container['securityContext']
        self.assertFalse(context['privileged'])
        self.assertFalse(context['allowPrivilegeEscalation'])
        self.assertEqual(context['capabilities']['drop'], ['ALL'])
        self.assertEqual(set(context['capabilities']['add']), CAPS)
        self.assertFalse(self.pod['automountServiceAccountToken'])

    def test_single_cpu_worker_writer_and_asset_readiness(self):
        self.assertEqual(self.deployment['spec']['replicas'], 1)
        self.assertEqual(self.deployment['spec']['strategy']['type'], 'Recreate')  # RWO volume
        terms = self.pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        self.assertEqual({e['key'] for e in terms['nodeSelectorTerms'][0]['matchExpressions']},
                         {'node-role.kubernetes.io/control-plane', 'nvidia.com/gpu.present'})
        self.assertIn('memory', self.container['resources']['limits'])
        for claim in self.by_kind['PersistentVolumeClaim']:
            self.assertEqual(claim['spec']['storageClassName'], 'ceph-block-retain')
            self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertEqual(self.container['readinessProbe']['exec']['command'][-1], '/netboot/ready.sh')
        self.assertEqual(self.container['command'], ['/bin/bash', '/netboot/entrypoint.sh'])
        for name in ('entrypoint.sh', 'ready.sh'):
            self.assertIn('. /netboot/lib.sh', self.data[name])
        self.assertIn('-o nbxyz -g nbxyz', self.data['entrypoint.sh'])  # dnsmasq --tftp-secure

    def test_config_and_assets_survive_restart_and_removal(self):
        claims = {c['metadata']['name']: c for c in self.by_kind['PersistentVolumeClaim']}
        self.assertEqual({n: c['spec']['resources']['requests']['storage'] for n, c in claims.items()},
                         {'netbootxyz-config': '2Gi', 'netbootxyz-assets': '50Gi'})
        for name, claim in claims.items():
            with self.subTest(claim=name):
                self.assertEqual(claim['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'],
                                 'disabled')
        volumes = {v['name']: v for v in self.pod['volumes']}
        mounts = {m['mountPath']: m['name'] for m in self.container['volumeMounts']}
        for path, claim in (('/config', 'netbootxyz-config'), ('/assets', 'netbootxyz-assets')):
            with self.subTest(path=path):
                self.assertEqual(volumes[mounts[path]].get('persistentVolumeClaim'),
                                 {'claimName': claim})
        self.assertFalse([v for v in self.pod['volumes'] if 'emptyDir' in v])

    def test_git_managed_menus_are_installed_read_only(self):
        """Configuration check only: the web app (nbxyz, no DAC_OVERRIDE) cannot save them."""
        for source, target in (('custom.ipxe', '"${menus}/custom/custom.ipxe"'),
                               ('local-vars.ipxe', '"${menus}/local-vars.ipxe"')):
            with self.subTest(file=source):
                self.assertRegex(self.data['entrypoint.sh'], r'(?m)install -m 0444 -o nbxyz -g nbxyz '
                                 + re.escape(f'/netboot/{source} {target}') + r' \\$')
                # Readiness still compares the served copy with the ConfigMap.
                self.assertIn(f'/netboot/{source}', self.data['ready.sh'])

    def test_release_check_rejects_error_pages_and_truncated_loaders(self):
        """Runs lib.sh's release(): what decides refetch, exit-to-retry and readiness."""
        good = {'menu.ipxe': b'#!ipxe\n', 'boot.cfg': b'#!ipxe\n', 'netboot.xyz.efi': pe_image()}
        loader = good['netboot.xyz.efi']
        broken = [('menu.ipxe', b'<html>404</html>'), ('boot.cfg', b''),
                  ('netboot.xyz.efi', loader[:-1]), ('netboot.xyz.efi', loader[:600]),
                  ('netboot.xyz.efi', loader[:88]),  # ends where the section table starts
                  ('netboot.xyz.efi', b'<html>404</html>'), ('netboot.xyz.efi', b'MZ' + bytes(2000))]
        for name, body in [(None, None)] + broken:
            with self.subTest(file=name, size=body and len(body)), \
                    tempfile.TemporaryDirectory() as tmp:
                pathlib.Path(tmp, 'lib.sh').write_text(self.data['lib.sh'])
                for file, content in {**good, **({name: body} if name else {})}.items():
                    pathlib.Path(tmp, file).write_bytes(content)
                code = subprocess.run(['bash', '-c', '. "$0/lib.sh"; release "$0"', tmp]).returncode
                self.assertEqual(code, 1 if name else 0)

    def test_every_exit_to_retry_check_also_refetches_on_the_next_start(self):
        lines = self.data['entrypoint.sh'].splitlines()
        checks = [set(re.findall(r'release "[^"]+"', next(s for s in lines if s.startswith(p))))
                  for p in ('if ! release', 'if [[ "$(cat /config/menuversion.txt')]
        self.assertLessEqual(*checks)  # else a damaged served copy crash-loops forever

    def test_talos_entry_needs_explicit_choice_and_passes_no_config(self):
        menu = self.data['custom.ipxe']
        self.assertTrue(menu.startswith('#!ipxe'))
        variables = dict(re.findall(r'^set (\S+) (\S+)$', menu, re.M))
        self.assertEqual(variables['talos_schematic'], SCHEMATIC)
        self.assertEqual(variables['talos_version'], 'v' + topf_talos_version())
        self.assertEqual(variables['talos_url'], 'https://pxe.factory.talos.dev/pxe/'
                         '${talos_schematic}/${talos_version}/metal-amd64')
        # Nothing boots Talos unless chosen: default is Back, and no timeout.
        self.assertRegex(menu, r'(?m)^choose --default back selected')
        self.assertNotIn('--timeout', menu)
        self.assertIn('chain --autofree ${talos_url} || goto failed', menu)
        # Only these iPXE commands run, so no config URL, disk boot or image edit.
        commands = {line.split()[0] for line in menu.splitlines()
                    if line.strip() and not line.startswith(('#', ':'))}
        self.assertEqual(commands, {'set', 'menu', 'item', 'choose', 'goto', 'echo', 'chain',
                                    'prompt', 'exit'})
        self.assertNotIn('talos.config', menu)
        self.assertIn('set custom_url tftp://${next-server}/custom', self.data['local-vars.ipxe'])


def pe_image(sections=((512, 512), (1024, 1000))):
    """Smallest PE layout pe() reads: DOS stub, COFF header, section table, section data."""
    head = bytearray(512)
    head[0:2] = b'MZ'
    struct.pack_into('<I', head, 60, 64)
    struct.pack_into('<4sHH12xHH', head, 64, b'PE\0\0', 0x8664, len(sections), 0, 0)
    for i, (pointer, size) in enumerate(sections):
        struct.pack_into('<16xII', head, 88 + 40 * i, size, pointer)
    return bytes(head) + bytes(max(p + s for p, s in sections) - len(head))


if __name__ == '__main__':
    unittest.main()
