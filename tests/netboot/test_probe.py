"""TFTP protocol contract for scripts/netboot/netboot_probe.py, against a local fake server."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('netboot_probe', ROOT / 'scripts/netboot/netboot_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def pe_image(payload):
    """A minimal PE32+ file: headers in the first 0x200 bytes, one section holding payload."""
    head = bytearray(0x200)
    head[:2] = b'MZ'
    struct.pack_into('<I', head, 0x3C, 0x40)  # e_lfanew
    head[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<HH12xH', head, 0x44, 0x8664, 1, 0xF0)  # x86-64, 1 section, optional size
    struct.pack_into('<H', head, 0x58, 0x20B)  # PE32+ optional header magic
    struct.pack_into('<II', head, 0x58 + 0xF0 + 16, len(payload), 0x200)  # raw size, raw offset
    return bytes(head) + payload


LOADER = pe_image(bytes(range(256)) * 39)  # 10496 bytes: several full blocks plus a short one
TALOS = 'https://pxe.factory.talos.dev/pxe/abc/v1.14.5/metal-amd64'
MENU = ('#!ipxe\nset talos_schematic abc\nset talos_version v1.14.5\n'
        'set talos_url https://pxe.factory.talos.dev/pxe/${talos_schematic}/${talos_version}/metal-amd64\n')


class FakeTftp(threading.Thread):
    """Serves read requests. single_port=False answers from a new port, like plain dnsmasq;
    other_port_for does that only for the named files; reply_host answers from another
    address; reply sends those raw bytes instead; delay waits before answering."""

    def __init__(self, files, single_port=True, honour_blksize=True, drop_first_data=False,
                 count=1, reply_host='127.0.0.1', reply=None, delay=0, oack_name=b'blksize',
                 other_port_for=()):
        super().__init__(daemon=True)
        self.files, self.single_port, self.count = files, single_port, count
        self.honour_blksize, self.drop_first_data = honour_blksize, drop_first_data
        self.reply_host, self.reply, self.delay = reply_host, reply, delay
        self.oack_name, self.other_port_for = oack_name, other_port_for
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.requests = []

    def run(self):
        with self.sock, contextlib.suppress(OSError):
            for _ in range(self.count):
                self.handle()

    def handle(self):
        packet, client = self.sock.recvfrom(65536)
        fields = packet[2:].split(b'\0')
        name, options = fields[0].decode(), dict(zip(fields[2::2], fields[3::2]))
        self.requests.append((struct.unpack('!H', packet[:2])[0], name))
        time.sleep(self.delay)
        if (self.single_port and name not in self.other_port_for
                and self.reply_host == '127.0.0.1'):
            return self.answer(self.sock, client, name, options)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((self.reply_host, 0))
            sock.settimeout(5)
            self.answer(sock, client, name, options)

    def answer(self, sock, client, name, options):
        if self.reply is not None:
            sock.sendto(self.reply, client)
            return
        if name not in self.files:
            sock.sendto(struct.pack('!HH', 5, 1) + b'File not found\0', client)
            return
        data, size = self.files[name], 512
        if self.honour_blksize and b'blksize' in options:
            size = int(options[b'blksize'])
            sock.sendto(struct.pack('!H', 6) + self.oack_name + b'\0' + str(size).encode() + b'\0',
                        client)
            sock.recvfrom(16)
        block, dropped = 1, False
        while True:
            chunk = data[(block - 1) * size:block * size]
            if self.drop_first_data and not dropped:
                dropped = True  # client must resend its last packet
                sock.recvfrom(16)
            sock.sendto(struct.pack('!HH', 3, block) + chunk, client)
            ack = sock.recvfrom(16)[0]
            if struct.unpack('!HH', ack[:4]) != (4, block):
                raise OSError(f'bad ACK for block {block}')
            if len(chunk) < size:
                return
            block += 1


class FakeHttp(threading.Thread):
    """Answers one GET. drip='headers' or 'body' sends the response from that point one
    byte every 0.05 s; the drip gives up after 4 s or when the client goes away."""

    def __init__(self, body=b'ok', drip=None):
        super().__init__(daemon=True)
        self.body, self.drip = body, drip
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        with self.sock, contextlib.suppress(OSError):
            conn, _ = self.sock.accept()
            with conn:
                conn.settimeout(5)
                conn.recv(65536)
                head = (b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                        % len(self.body))
                response = head + self.body
                start = {None: len(response), 'headers': 0, 'body': len(head)}[self.drip]
                conn.sendall(response[:start])
                stop = time.monotonic() + 4
                for index in range(start, len(response)):
                    if time.monotonic() > stop:
                        break
                    time.sleep(0.05)
                    conn.sendall(response[index:index + 1])


class ProbeProtocol(unittest.TestCase):
    def serve(self, files, **kwargs):
        server = FakeTftp(files, **kwargs)
        server.start()
        self.addCleanup(server.join, 6)  # the thread closes its sockets on exit
        return server

    def fetch(self, server, name='netboot.xyz.efi', timeout=0.5, retries=4):
        return probe.tftp_get('127.0.0.1', server.port, name, timeout=timeout, retries=retries,
                              deadline=10)

    def test_complete_transfer_with_negotiated_block_size(self):
        server = self.serve({'netboot.xyz.efi': LOADER})
        body, origin = self.fetch(server)
        self.assertEqual(body, LOADER)
        self.assertEqual(origin[1], server.port)
        self.assertEqual(server.requests, [(1, 'netboot.xyz.efi')])  # read request only

    def test_plain_512_byte_blocks_and_exact_multiple(self):
        exact = b'MZ' + b'x' * 1022  # two full blocks, then an empty final block
        body, _ = self.fetch(self.serve({'netboot.xyz.efi': exact}, honour_blksize=False))
        self.assertEqual(body, exact)

    def test_lost_packet_is_retried(self):
        body, _ = self.fetch(self.serve({'netboot.xyz.efi': LOADER}, drop_first_data=True))
        self.assertEqual(body, LOADER)

    def test_server_error_is_reported(self):
        with self.assertRaisesRegex(probe.ProbeError, 'TFTP error 1'):
            self.fetch(self.serve({}))

    def test_silent_server_fails_within_bounds(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
            silent.bind(('127.0.0.1', 0))
            with self.assertRaisesRegex(probe.ProbeError, 'no reply'):
                probe.tftp_get('127.0.0.1', silent.getsockname()[1], 'x', timeout=0.2, retries=2)

    def test_data_from_another_host_is_ignored(self):
        server = self.serve({}, reply_host='127.0.0.2', reply=struct.pack('!HH', 3, 1) + b'MZ')
        with self.assertRaisesRegex(probe.ProbeError, 'no reply'):
            self.fetch(server, timeout=0.2, retries=2)

    def test_reply_after_the_deadline_fails(self):
        # The reply would finish the transfer, but it lands after the deadline and inside
        # a single receive timeout.
        server = self.serve({}, delay=1.0, reply=struct.pack('!HH', 3, 1) + b'MZ')
        started = time.monotonic()
        with self.assertRaisesRegex(probe.ProbeError, 'deadline'):
            probe.tftp_get('127.0.0.1', server.port, 'x', timeout=3, retries=1, deadline=0.3)
        self.assertLess(time.monotonic() - started, 0.9)

    def test_upper_case_oack_option_is_honoured(self):
        body, _ = self.fetch(self.serve({'netboot.xyz.efi': LOADER}, oack_name=b'BLKSIZE'))
        self.assertEqual(body, LOADER)

    def test_malformed_replies_fail_cleanly(self):
        cases = ((b'\0', 'malformed 1-byte packet'),
                 (struct.pack('!H', 6) + b'blksize\x000\0', 'offered blksize 0'),
                 (struct.pack('!H', 6) + b'blksize\x0065464\0', 'offered blksize 65464'))
        for reply, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(probe.ProbeError, message):
                self.fetch(self.serve({}, reply=reply))

    def run_probe(self, server, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probe.run(probe.parse_args(
                ['--host', '127.0.0.1', '--tftp-port', str(server.port), '--http-port', '0',
                 '--timeout', '0.5', '--deadline', '10', *extra]))
        return code, out.getvalue()

    def reference(self, data):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_probe_passes_on_identical_loader(self):
        code, out = self.run_probe(self.serve({'netboot.xyz.efi': LOADER}),
                                   '--reference', self.reference(LOADER))
        self.assertEqual(code, 0, out)
        self.assertIn(hashlib.sha256(LOADER).hexdigest(), out)

    def test_probe_fails_on_truncated_or_html_loader(self):
        for served in (LOADER[:-1], b'<html>404</html>'):
            with self.subTest(size=len(served)):
                code, out = self.run_probe(self.serve({'netboot.xyz.efi': served}),
                                           '--reference', self.reference(LOADER))
                self.assertEqual(code, 1, out)

    def test_probe_reports_malformed_reply_as_failed_check(self):
        code, out = self.run_probe(self.serve({}, reply=b'\0'))
        self.assertEqual(code, 1)
        self.assertIn('malformed 1-byte packet', out)

    def test_probe_requires_single_port_replies(self):
        code, out = self.run_probe(self.serve({'netboot.xyz.efi': LOADER}, single_port=False))
        self.assertEqual(code, 1)
        self.assertIn('data came from port', out)

    def test_probe_checks_talos_url_in_menu(self):
        files = {'netboot.xyz.efi': LOADER, 'custom/custom.ipxe': MENU.encode()}
        server = self.serve(files, count=2)  # loader, then menu
        code, out = self.run_probe(server, '--expect-talos-url', TALOS)
        self.assertEqual(code, 0, out)
        self.assertIn(TALOS, out)

    def test_probe_rejects_incomplete_pe_without_reference(self):
        for served in (b'MZ', LOADER[:-1], LOADER[:0x150]):
            with self.subTest(size=len(served)):
                code, out = self.run_probe(self.serve({'netboot.xyz.efi': served}))
                self.assertEqual(code, 1, out)
                self.assertIn('not a complete PE file', out)

    def test_probe_requires_single_port_for_the_menu(self):
        files = {'netboot.xyz.efi': LOADER, 'custom/custom.ipxe': MENU.encode()}
        server = self.serve(files, count=2, other_port_for={'custom/custom.ipxe'})
        code, out = self.run_probe(server, '--expect-talos-url', TALOS)
        checks = json.loads(out)['checks']
        self.assertEqual(code, 1, out)
        self.assertTrue(checks['tftp_loader']['ok'])
        self.assertIn('data came from port', checks['custom_menu']['error'])

        server = self.serve(files, count=2, other_port_for={'custom/custom.ipxe'})
        code, out = self.run_probe(server, '--expect-talos-url', TALOS, '--allow-other-port')
        self.assertEqual(code, 0, out)

    def test_probe_bounds_menu_variable_expansion(self):
        bomb = 'set x xxxxxxxx\n' + 'set x ${x}${x}\n' * 30 + 'set talos_url ' + TALOS + '\n'
        files = {'netboot.xyz.efi': LOADER, 'custom/custom.ipxe': bomb.encode()}
        code, out = self.run_probe(self.serve(files, count=2), '--expect-talos-url', TALOS)
        self.assertEqual(code, 1, out)
        self.assertIn('menu variables expand past', json.loads(out)['checks']['custom_menu']['error'])


class HttpDeadline(unittest.TestCase):
    def serve(self, **kwargs):
        server = FakeHttp(**kwargs)
        server.start()
        self.addCleanup(server.join, 6)
        return server

    def test_prompt_response_is_returned(self):
        server = self.serve(body=b'hello')
        self.assertEqual(probe.http_get(f'http://127.0.0.1:{server.port}/', timeout=1, deadline=3),
                         (200, b'hello'))

    def test_dripped_response_stops_at_the_deadline(self):
        # Each byte arrives well inside the socket timeout, so only a whole-request
        # deadline stops the read.
        for drip in ('headers', 'body'):
            with self.subTest(drip=drip):
                server = self.serve(body=b'x' * 1000, drip=drip)
                started = time.monotonic()
                with self.assertRaisesRegex(probe.ProbeError, 'no complete response within'):
                    probe.http_get(f'http://127.0.0.1:{server.port}/', timeout=1, deadline=0.5)
                self.assertLess(time.monotonic() - started, 2)


class CommandLine(unittest.TestCase):
    def rejected(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            probe.parse_args(['--host', '127.0.0.1', *argv])
        self.assertEqual(exit_.exception.code, 2, err.getvalue())
        return err.getvalue()

    def test_defaults(self):
        args = probe.parse_args(['--host', '127.0.0.1'])
        self.assertEqual((args.tftp_port, args.http_port, args.timeout, args.deadline),
                         (69, 8080, 3.0, 60.0))

    def test_rejects_unbounded_or_unusable_times(self):
        for flag in ('--timeout', '--deadline'):
            for value in ('nan', 'inf', '-inf', '1e400', '0', '-0', '-1', '86401', 'soon'):
                with self.subTest(flag=flag, value=value):
                    self.assertIn(flag, self.rejected(flag, value))

    def test_accepts_boundary_times(self):
        for value, expected in (('0.001', 0.001), ('86400', 86400.0)):
            with self.subTest(value=value):
                args = probe.parse_args(['--host', 'h', '--timeout', value, '--deadline', value])
                self.assertEqual((args.timeout, args.deadline), (expected, expected))

    def test_rejects_invalid_ports(self):
        cases = (('--tftp-port', '0'), ('--tftp-port', '-1'), ('--tftp-port', '65536'),
                 ('--tftp-port', '70000'), ('--tftp-port', 'tftp'), ('--http-port', '-1'),
                 ('--http-port', '65536'), ('--http-port', '8080.5'))
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                self.assertIn(flag, self.rejected(flag, value))

    def test_accepts_boundary_ports(self):
        for tftp, http in (('1', '0'), ('65535', '65535')):
            with self.subTest(tftp=tftp, http=http):
                args = probe.parse_args(['--host', 'h', '--tftp-port', tftp, '--http-port', http])
                self.assertEqual((args.tftp_port, args.http_port), (int(tftp), int(http)))

    def test_script_reports_bad_port_as_usage_error(self):
        # Before the fix this reached sendto and died with a traceback and no JSON.
        result = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/netboot/netboot_probe.py'), '--host', '127.0.0.1',
             '--tftp-port', '70000', '--http-port', '0'],
            capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('--tftp-port', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
