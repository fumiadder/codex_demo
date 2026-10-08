#!/usr/bin/env python3
"""Read-only production HTTPS/config/header check; never creates accounts."""
import argparse
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Unexpected production redirect')


def run(url, budget):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('An HTTPS origin is required')
    opener = urllib.request.build_opener(NoRedirect())
    end = time.monotonic()+budget
    while True:
        try:
            for path in ('/api/health','/api/config','/'):
                with opener.open(url.rstrip('/')+path, timeout=min(10,max(1,end-time.monotonic()))) as response:
                    if response.status != 200 or response.headers.get('X-Content-Type-Options') != 'nosniff' or not response.headers.get('Strict-Transport-Security') or "frame-ancestors 'none'" not in response.headers.get('Content-Security-Policy',''):
                        raise ValueError('Production response/security headers failed')
                    body = response.read(1024*1024)
                    if path == '/api/health' and json.loads(body).get('ok') is not True:
                        raise ValueError('Health endpoint failed')
                    if path == '/api/config':
                        config = json.loads(body)
                        if config.get('testMode') is not False or config.get('storagePersistence') != 'persistent' or config.get('registrationMode') != 'invite':
                            raise ValueError('Production invite/persistence configuration failed')
            print('PASS read-only production HTTPS, health, config, and security headers')
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            if time.monotonic() >= end:
                raise ValueError('Production HTTPS readiness timed out') from None
            time.sleep(min(3,max(0,end-time.monotonic())))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--timeout',type=int,default=120)
    args=parser.parse_args()
    try:
        if not 1 <= args.timeout <= 180:
            raise ValueError('Invalid readiness timeout')
        run(args.url,args.timeout)
    except Exception as error:
        print('FAIL production verification: '+type(error).__name__)
        raise SystemExit(1)
