#!/usr/bin/env python3
"""Read-only probe for the netboot.xyz PXE endpoint.

Downloads the UEFI loader over TFTP (read requests only, never writes), checks
it is a complete PE file identical to a reference copy, checks the TFTP data
came from UDP 69 (single-port mode, which the load balancer relies on), reads
the custom menu and confirms it boots the pinned Talos image, and checks the
asset web server answers. Every network step has a deadline and a size cap.

  scripts/netboot/netboot_probe.py --host <pxe-service-address> \
      --reference https://github.com/netbootxyz/netboot.xyz/releases/download/3.0.3/netboot.xyz.efi \
      --expect-talos-url https://pxe.factory.talos.dev/pxe/<schematic>/v1.14.5/metal-amd64

Exit 0 when every check passes, 1 otherwise. Prints one JSON summary.
"""

import argparse
import contextlib
import hashlib
import http.client
import json
import math
import re
import socket
import struct
import sys
import threading
import time
import urllib.request

RRQ, DATA, ACK, ERROR, OACK = 1, 3, 4, 5, 6
MAX_BYTES = 16 * 1024 * 1024
MAX_MENU_BYTES = 64 * 1024  # caps the menu download and its expanded variables
MAX_SECONDS = 86400.0  # one day: far inside what socket and thread timeouts accept
VARIABLE = re.compile(r'\$\{([^}]*)\}')


class ProbeError(Exception):
    pass


def tftp_get(host, port, filename, blksize=1468, timeout=3.0, retries=5,
             deadline=60.0, max_bytes=MAX_BYTES):
    """Fetch one file in octet mode. Returns (bytes, server (ip, port)).
    The deadline includes the name lookup, which cannot be interrupted: a lookup
    that returns late fails the transfer with the deadline error."""
    end = time.monotonic() + deadline
    address = socket.gethostbyname(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    request = (struct.pack('!H', RRQ) + filename.encode() + b'\0octet\0'
               + b'blksize\0' + str(blksize).encode() + b'\0')
    last, dest = request, (address, port)
    server, size, expected, data, misses = None, 512, 1, bytearray(), 0
    try:
        sock.sendto(last, dest)
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise ProbeError(f'{filename}: deadline of {deadline}s passed')
            # No single wait may run past the deadline, so a late final block fails.
            sock.settimeout(min(timeout, remaining))
            try:
                packet, addr = sock.recvfrom(65536)
            except socket.timeout:
                if time.monotonic() >= end:
                    continue  # reported as a deadline failure, not a lost packet
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
                # Option names are case-insensitive (RFC 2347). The trailing NUL
                # leaves an odd field count, so zip must not be strict here.
                options = dict(zip((name.lower() for name in fields[0::2]), fields[1::2],
                                   strict=False))
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


def http_get(url, timeout=10.0, deadline=60.0, max_bytes=MAX_BYTES):
    """GET an http(s) URL. timeout bounds each socket wait; deadline bounds the
    whole request: connection setup, redirects, headers and body. A name lookup
    cannot be interrupted, so one that returns late fails the request instead."""
    sockets, expired = [], threading.Event()

    def cut(sock):
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read with EOF

    def connect_within(address, timeout, source_address=None):
        # socket.create_connection, but each resolved address gets only the time
        # left, so connection setup cannot outlast the deadline.
        error = TimeoutError(f'no connection within {deadline}s')
        for *_, sockaddr in socket.getaddrinfo(*address, type=socket.SOCK_STREAM):
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                sock = socket.create_connection(sockaddr[:2], min(timeout, remaining),
                                                source_address)
            except OSError as exc:
                error = exc
                continue
            sock.settimeout(timeout)
            return sock
        raise error

    def tracked(base):
        class Connection(base):
            def connect(self):
                self._create_connection = connect_within
                super().connect()
                sockets.append(self.sock)
                if expired.is_set():
                    cut(self.sock)
        return Connection

    class Http(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(tracked(http.client.HTTPConnection), req)

    class Https(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(tracked(http.client.HTTPSConnection), req,
                                context=self._context)

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None:
                fp.close()  # the stock handler would read the whole redirect body
            return new

    def expire():
        expired.set()
        for sock in list(sockets):
            cut(sock)

    # Only http and https: any other scheme, including a redirect to one, fails.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), Http(), Https(), Redirect(),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler()):
        opener.add_handler(handler)
    end = time.monotonic() + deadline
    timer = threading.Timer(deadline, expire)
    timer.start()
    try:
        with opener.open(url, timeout=min(timeout, deadline)) as response:
            body = response.read(max_bytes + 1)
            status, missing = response.status, response.length
    except (OSError, http.client.HTTPException):
        if expired.is_set() or time.monotonic() >= end:
            raise ProbeError(f'{url}: no complete response within {deadline}s') from None
        raise
    finally:
        timer.cancel()
        timer.join()
    if expired.is_set():  # a cut connection can look like a clean, short body
        raise ProbeError(f'{url}: no complete response within {deadline}s')
    if len(body) > max_bytes:
        raise ProbeError(f'{url}: larger than {max_bytes} bytes')
    if missing:  # bytes still owed under Content-Length; None when no length was sent
        raise ProbeError(f'{url}: response ended {missing} bytes short of its Content-Length')
    return status, body


def load_reference(reference, deadline=60.0):
    if reference.startswith(('http://', 'https://')):
        status, body = http_get(reference, timeout=30.0, deadline=deadline)
        if status != 200:
            raise ProbeError(f'reference {reference}: HTTP {status}')
        return body
    with open(reference, 'rb') as handle:
        return handle.read(MAX_BYTES + 1)


def check_pe(body, name):
    """Raise unless body is a PE image whose headers and section data fit inside it."""
    def fail(reason):
        raise ProbeError(f'{name} is not a complete PE file: {reason}')

    if len(body) < 0x40 or body[:2] != b'MZ':
        fail('no MZ header')
    pe = struct.unpack_from('<I', body, 0x3C)[0]
    if pe + 24 > len(body) or body[pe:pe + 4] != b'PE\0\0':
        fail('no PE signature')
    sections, optional = struct.unpack_from('<H12xH', body, pe + 6)
    table = pe + 24 + optional
    if sections == 0 or optional < 2 or table + 40 * sections > len(body):
        fail('section table missing or cut off')
    if struct.unpack_from('<H', body, pe + 24)[0] not in (0x10B, 0x20B):
        fail('unknown optional header')
    for index in range(sections):
        size, offset = struct.unpack_from('<II', body, table + 40 * index + 16)
        if size and offset + size > len(body):
            fail(f'section {index} ends at byte {offset + size} of {len(body)}')


def menu_variables(text, limit=MAX_MENU_BYTES):
    """Expand `set name value` lines in order. Unknown ${...} references stay as
    written. The expanded values together may not exceed limit characters."""
    variables, retained = {}, 0

    def lookup(match):
        return variables.get(match[1], match[0])

    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] != 'set':
            continue
        name, raw = parts[1], parts[2]
        size = len(raw) + sum(len(lookup(m)) - len(m[0]) for m in VARIABLE.finditer(raw))
        retained += size - len(variables.get(name, ''))
        if retained > limit:  # checked before the expanded string is built
            raise ProbeError(f'menu variables expand past {limit} characters')
        variables[name] = VARIABLE.sub(lookup, raw)
    return variables


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

    def require_port(server):
        if args.require_single_port and server[1] != args.tftp_port:
            raise ProbeError(f'data came from port {server[1]}, not {args.tftp_port}')

    def tftp_loader():
        body, server = tftp_get(args.host, args.tftp_port, args.loader,
                                timeout=args.timeout, deadline=args.deadline)
        loader['body'] = body
        check_pe(body, args.loader)
        require_port(server)
        return {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                'server_port': server[1]}

    check('tftp_loader', tftp_loader)

    if args.reference:
        def compare():
            if 'body' not in loader:
                raise ProbeError('no TFTP copy to compare')
            reference = load_reference(args.reference, deadline=args.deadline)
            got, want = loader['body'], reference
            if got != want:
                raise ProbeError(f'TFTP copy ({len(got)} bytes) differs from the '
                                 f'reference ({len(want)} bytes)')
            return {'bytes': len(want), 'sha256': hashlib.sha256(want).hexdigest()}
        check('loader_matches_reference', compare)

    if args.expect_talos_url:
        def menu():
            body, server = tftp_get(args.host, args.tftp_port, args.menu, timeout=args.timeout,
                                    deadline=args.deadline, max_bytes=MAX_MENU_BYTES)
            require_port(server)
            variables = menu_variables(body.decode(errors='replace'))
            if variables.get('talos_url') != args.expect_talos_url:
                raise ProbeError(f'menu boots {variables.get("talos_url")!r}')
            return {'talos_url': variables['talos_url'], 'server_port': server[1]}
        check('custom_menu', menu)

    if args.http_port:
        def http_root():
            status, _ = http_get(f'http://{args.host}:{args.http_port}/', timeout=args.timeout * 3,
                                 deadline=args.deadline)
            if status != 200:
                raise ProbeError(f'HTTP {status}')
            return {'status': status}
        check('http', http_root)

    print(json.dumps({'host': args.host, 'ok': ok, 'checks': results}, indent=2))
    return 0 if ok else 1


def seconds(text):
    """argparse type: a finite, positive number of seconds, at most MAX_SECONDS."""
    value = float(text)
    if not math.isfinite(value) or not 0 < value <= MAX_SECONDS:
        raise argparse.ArgumentTypeError(
            f'{text!r} is not a number of seconds above 0 and at most {MAX_SECONDS}')
    return value


def port(lowest):
    """argparse type: an integer port from lowest to 65535."""
    def parse(text):
        value = int(text)
        if not lowest <= value <= 65535:
            raise argparse.ArgumentTypeError(f'{text!r} is not a port from {lowest} to 65535')
        return value
    parse.__name__ = 'port'  # argparse names the type in its "invalid value" message
    return parse


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--host', required=True)
    parser.add_argument('--tftp-port', type=port(1), default=69)
    parser.add_argument('--http-port', type=port(0), default=8080,
                        help='0 skips the HTTP check')
    parser.add_argument('--loader', default='netboot.xyz.efi')
    parser.add_argument('--menu', default='custom/custom.ipxe')
    parser.add_argument('--reference', help='URL or path of the expected loader')
    parser.add_argument('--expect-talos-url')
    parser.add_argument('--allow-other-port', dest='require_single_port',
                        action='store_false', help='accept TFTP data from a non-69 port')
    parser.add_argument('--timeout', type=seconds, default=3.0,
                        help='seconds to wait for any one reply')
    parser.add_argument('--deadline', type=seconds, default=60.0,
                        help='seconds allowed for each check')
    return parser.parse_args(argv)


if __name__ == '__main__':
    sys.exit(run(parse_args(sys.argv[1:])))
