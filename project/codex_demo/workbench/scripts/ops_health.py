#!/usr/bin/env python3
"""Read-only production HTTPS/config/header check; never creates accounts."""
import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Unexpected production redirect')


def run(url, budget, insecure=False):
    parsed = urlsplit(url)
    # A temporary IP-only server may be served over plain HTTP; both schemes are
    # accepted, but HSTS is only required when the origin is HTTPS.
    secure = parsed.scheme == 'https'
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('An http(s) origin is required')
    handlers = [NoRedirect()]
    if insecure:
        # Only used for an IP-only temporary server with Caddy's internal CA.
        # The response body and security headers are still fully verified.
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)
    end = time.monotonic()+budget
    while True:
        try:
            for path in ('/api/health','/api/config','/'):
                with opener.open(url.rstrip('/')+path, timeout=min(10,max(1,end-time.monotonic()))) as response:
                    headers_ok = (
                        response.status == 200
                        and response.headers.get('X-Content-Type-Options') == 'nosniff'
                        and "frame-ancestors 'none'" in response.headers.get('Content-Security-Policy', '')
                    )
                    if secure:
                        headers_ok = headers_ok and bool(response.headers.get('Strict-Transport-Security'))
                    if not headers_ok:
                        raise ValueError('Production response/security headers failed')
                    body = response.read(1024*1024)
                    if path == '/api/health' and json.loads(body).get('ok') is not True:
                        raise ValueError('Health endpoint failed')
                    if path == '/api/config':
                        config = json.loads(body)
                        if config.get('testMode') is not False or config.get('storagePersistence') != 'persistent' or config.get('registrationMode') != 'invite':
                            raise ValueError('Production invite/persistence configuration failed')
            print(f'PASS read-only production {parsed.scheme.upper()}, health, config, and security headers')
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            if time.monotonic() >= end:
                raise ValueError('Production HTTPS readiness timed out') from None
            time.sleep(min(3,max(0,end-time.monotonic())))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--timeout',type=int,default=120)
    parser.add_argument('--insecure',action='store_true',
                        help='skip TLS certificate verification (IP-only internal-CA servers only)')
    args=parser.parse_args()
    try:
        if not 1 <= args.timeout <= 180:
            raise ValueError('Invalid readiness timeout')
        run(args.url,args.timeout,args.insecure)
    except Exception as error:
        print('FAIL production verification: '+type(error).__name__)
        raise SystemExit(1)
