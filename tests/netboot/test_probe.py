"""TFTP protocol contract for scripts/netboot/netboot_probe.py, against a local fake server."""

import contextlib
import hashlib
import importlib.util
import io
import os
import pathlib
import socket
import struct
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('netboot_probe', ROOT / 'scripts/netboot/netboot_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

LOADER = b'MZ' + bytes(range(256)) * 40  # 10242 bytes: several full blocks plus a short one
TALOS = 'https://pxe.factory.talos.dev/pxe/abc/v1.14.5/metal-amd64'
MENU = ('#!ipxe\nset talos_schematic abc\nset talos_version v1.14.5\n'
        'set talos_url https://pxe.factory.talos.dev/pxe/${talos_schematic}/${talos_version}/metal-amd64\n')


class FakeTftp(threading.Thread):
    """Serves read requests. single_port=False answers from a new port, like plain dnsmasq;
    reply_host answers from another address; reply sends those raw bytes instead."""

    def __init__(self, files, single_port=True, honour_blksize=True, drop_first_data=False,
                 count=1, reply_host='127.0.0.1', reply=None):
        super().__init__(daemon=True)
        self.files, self.single_port, self.count = files, single_port, count
        self.honour_blksize, self.drop_first_data = honour_blksize, drop_first_data
        self.reply_host, self.reply = reply_host, reply
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
        if self.single_port and self.reply_host == '127.0.0.1':
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
            sock.sendto(struct.pack('!H', 6) + b'blksize\0' + str(size).encode() + b'\0', client)
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


if __name__ == '__main__':
    unittest.main()
