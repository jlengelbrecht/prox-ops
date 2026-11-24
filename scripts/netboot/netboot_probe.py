#!/usr/bin/env python3
"""Read-only probe for the netboot.xyz PXE endpoint.

Downloads the UEFI loader over TFTP (read requests only, never writes), checks
it is a complete PE file identical to a reference copy, checks the TFTP data
came from UDP 69 (single-port mode, which the load balancer relies on), reads
the custom menu and confirms it boots the pinned Talos image, and checks the
asset web server answers. Every network step has a timeout and a size cap.

  scripts/netboot/netboot_probe.py --host 10.20.67.28 \
      --reference https://github.com/netbootxyz/netboot.xyz/releases/download/3.0.3/netboot.xyz.efi \
      --expect-talos-url https://pxe.factory.talos.dev/pxe/<schematic>/v1.14.5/metal-amd64

Exit 0 when every check passes, 1 otherwise. Prints one JSON summary.
"""

import argparse
import hashlib
import http.client
import json
import socket
import struct
import sys
import time
import urllib.request

RRQ, DATA, ACK, ERROR, OACK = 1, 3, 4, 5, 6
MAX_BYTES = 16 * 1024 * 1024


class ProbeError(Exception):
    pass


def tftp_get(host, port, filename, blksize=1468, timeout=3.0, retries=5,
             deadline=60.0, max_bytes=MAX_BYTES):
    """Fetch one file in octet mode. Returns (bytes, server (ip, port))."""
    address = socket.gethostbyname(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    end = time.monotonic() + deadline
    request = (struct.pack('!H', RRQ) + filename.encode() + b'\0octet\0'
               + b'blksize\0' + str(blksize).encode() + b'\0')
    last, dest = request, (address, port)
    server, size, expected, data, misses = None, 512, 1, bytearray(), 0
    try:
        sock.sendto(last, dest)
        while True:
            if time.monotonic() > end:
                raise ProbeError(f'{filename}: deadline of {deadline}s passed')
            try:
                packet, addr = sock.recvfrom(65536)
            except socket.timeout:
                misses += 1
                if misses > retries:
                    raise ProbeError(f'{filename}: no reply after {retries} retries')
                sock.sendto(last, dest)
                continue
            if server is None and addr[0] == address:
                server = addr
            elif addr != server:
                # Another host, or a stray port, is ignored; a stray port on the
                # server is told off (RFC 1350, error 5).
                if addr[0] == address:
                    sock.sendto(struct.pack('!HH', ERROR, 5) + b'unknown transfer id\0', addr)
                continue
            misses = 0
            if len(packet) < 4:
                raise ProbeError(f'{filename}: malformed {len(packet)}-byte packet')
            opcode = struct.unpack('!H', packet[:2])[0]
            if opcode == ERROR:
                code = struct.unpack('!H', packet[2:4])[0]
                raise ProbeError(f'{filename}: TFTP error {code}: '
                                 f'{packet[4:].rstrip(b"\0").decode(errors="replace")}')
            if opcode == OACK and expected == 1 and not data:
                fields = packet[2:].split(b'\0')
                options = dict(zip(fields[0::2], fields[1::2]))
                size = int(options.get(b'blksize', b'512'))
                if not 8 <= size <= blksize:  # RFC 2348: never above the request
                    raise ProbeError(f'{filename}: server offered blksize {size}')
                last, dest = struct.pack('!HH', ACK, 0), server
                sock.sendto(last, dest)
                continue
            if opcode != DATA:
                raise ProbeError(f'{filename}: unexpected opcode {opcode}')
            block = struct.unpack('!H', packet[2:4])[0]
            payload = packet[4:]
            if block == expected & 0xFFFF:
                data += payload
                if len(data) > max_bytes:
                    raise ProbeError(f'{filename}: larger than {max_bytes} bytes')
                last, dest = struct.pack('!HH', ACK, block), server
                sock.sendto(last, dest)
                expected += 1
                if len(payload) < size:
                    return bytes(data), server
            elif block == (expected - 1) & 0xFFFF:
                sock.sendto(struct.pack('!HH', ACK, block), server)
    finally:
        sock.close()


def http_get(url, timeout=10.0, max_bytes=MAX_BYTES):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ProbeError(f'{url}: larger than {max_bytes} bytes')
        return response.status, body


def load_reference(reference):
    if reference.startswith(('http://', 'https://')):
        status, body = http_get(reference, timeout=30.0)
        if status != 200:
            raise ProbeError(f'reference {reference}: HTTP {status}')
        return body
    with open(reference, 'rb') as handle:
        return handle.read(MAX_BYTES + 1)


def run(args):
    results, ok = {}, True

    def check(name, func):
        nonlocal ok
        try:
            results[name] = {'ok': True, **func()}
        except (ProbeError, OSError, ValueError, http.client.HTTPException) as error:
            ok = False
            results[name] = {'ok': False, 'error': str(error)}

    loader = {}

    def tftp_loader():
        body, server = tftp_get(args.host, args.tftp_port, args.loader,
                                timeout=args.timeout, deadline=args.deadline)
        loader['body'] = body
        if body[:2] != b'MZ':
            raise ProbeError(f'{args.loader} is not a PE file')
        if args.require_single_port and server[1] != args.tftp_port:
            raise ProbeError(f'data came from port {server[1]}, not {args.tftp_port}')
        return {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                'server_port': server[1]}

    check('tftp_loader', tftp_loader)

    if args.reference:
        def compare():
            if 'body' not in loader:
                raise ProbeError('no TFTP copy to compare')
            reference = load_reference(args.reference)
            got, want = loader['body'], reference
            if got != want:
                raise ProbeError(f'TFTP copy ({len(got)} bytes) differs from the '
                                 f'reference ({len(want)} bytes)')
            return {'bytes': len(want), 'sha256': hashlib.sha256(want).hexdigest()}
        check('loader_matches_reference', compare)

    if args.expect_talos_url:
        def menu():
            body, _ = tftp_get(args.host, args.tftp_port, args.menu,
                               timeout=args.timeout, deadline=args.deadline)
            text = body.decode(errors='replace')
            variables = {}
            for line in text.splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[0] == 'set':
                    value = parts[2]
                    for name, known in variables.items():
                        value = value.replace('${' + name + '}', known)
                    variables[parts[1]] = value
            if variables.get('talos_url') != args.expect_talos_url:
                raise ProbeError(f'menu boots {variables.get("talos_url")!r}')
            return {'talos_url': variables['talos_url']}
        check('custom_menu', menu)

    if args.http_port:
        def http_root():
            status, _ = http_get(f'http://{args.host}:{args.http_port}/', timeout=args.timeout * 3)
            if status != 200:
                raise ProbeError(f'HTTP {status}')
            return {'status': status}
        check('http', http_root)

    print(json.dumps({'host': args.host, 'ok': ok, 'checks': results}, indent=2))
    return 0 if ok else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--host', required=True)
    parser.add_argument('--tftp-port', type=int, default=69)
    parser.add_argument('--http-port', type=int, default=8080,
                        help='0 skips the HTTP check')
    parser.add_argument('--loader', default='netboot.xyz.efi')
    parser.add_argument('--menu', default='custom/custom.ipxe')
    parser.add_argument('--reference', help='URL or path of the expected loader')
    parser.add_argument('--expect-talos-url')
    parser.add_argument('--allow-other-port', dest='require_single_port',
                        action='store_false', help='accept TFTP data from a non-69 port')
    parser.add_argument('--timeout', type=float, default=3.0)
    parser.add_argument('--deadline', type=float, default=60.0)
    return parser.parse_args(argv)


if __name__ == '__main__':
    sys.exit(run(parse_args(sys.argv[1:])))
