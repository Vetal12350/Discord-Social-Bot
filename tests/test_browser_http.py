"""No-network regression checks for guarded TikTok Chrome impersonation."""
import socket
import unittest
from unittest.mock import patch

from browser_http import guarded_curl_request, pinned_address


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
               ('93.184.215.14', 443))]


class Response:
    def __init__(self, code=200, location=None):
        self.status_code = code
        self.headers = {'Location': location} if location else {}
        self.closed = False

    def close(self):
        self.closed = True


class CurlGuardTests(unittest.TestCase):
    def test_public_ip_is_pinned_to_curl_without_proxy_or_native_redirects(self):
        calls = []

        def request(session, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

        with patch('browser_http.socket.getaddrinfo', return_value=PUBLIC_DNS):
            response = guarded_curl_request(
                request, object(), 'GET', 'https://www.tiktok.com/@test/video/123',
                resolve_option='DNS_PIN', impersonate='chrome', stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]['curl_options']['DNS_PIN'],
                         ['www.tiktok.com:443:93.184.215.14'])
        self.assertIs(calls[0][2]['allow_redirects'], False)
        self.assertEqual(calls[0][2]['max_redirects'], 0)
        self.assertEqual(calls[0][2]['proxy'], '')
        self.assertEqual(calls[0][2]['proxies'], {})
        self.assertEqual(calls[0][2]['impersonate'], 'chrome')
        self.assertTrue(calls[0][2]['stream'])

    def test_tiktok_redirect_is_validated_and_resolved_again(self):
        calls = []
        first = Response(302, 'https://www.tiktok.com/@user/video/123')

        def request(session, method, url, **kwargs):
            calls.append((url, kwargs))
            return first if len(calls) == 1 else Response(200)

        with patch('browser_http.socket.getaddrinfo', return_value=PUBLIC_DNS):
            output = guarded_curl_request(request, object(), 'GET',
                                          'https://vt.tiktok.com/ABC',
                                          resolve_option='PIN')
        self.assertEqual(output.status_code, 200)
        self.assertTrue(first.closed)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]['curl_options']['PIN'],
                         ['vt.tiktok.com:443:93.184.215.14'])
        self.assertEqual(calls[1][1]['curl_options']['PIN'],
                         ['www.tiktok.com:443:93.184.215.14'])

    def test_private_dns_is_rejected_before_native_socket(self):
        request = lambda *_a, **_kw: self.fail('No native connection allowed')
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
            result = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
            with self.subTest(ip=ip):
                with patch('browser_http.socket.getaddrinfo', return_value=result):
                    with self.assertRaises(OSError):
                        guarded_curl_request(request, object(), 'GET',
                                             'https://www.tiktok.com/',
                                             resolve_option='PIN')

    def test_mixed_public_and_private_dns_fails_closed(self):
        mixed = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                               ('127.0.0.1', 443))]
        with patch('browser_http.socket.getaddrinfo', return_value=mixed):
            with self.assertRaises(OSError):
                pinned_address('www.tiktok.com')

    def test_unsupported_destination_rejected_without_dns(self):
        request = lambda *_a, **_kw: self.fail('No native request allowed')
        for target in ('http://www.tiktok.com/', 'https://127.0.0.1/',
                       'https://www.tiktok.com:443/', 'https://evil.example/video',
                       'https://user:pass@www.tiktok.com/video'):
            with self.subTest(target=target):
                with patch('browser_http.socket.getaddrinfo') as dns:
                    with self.assertRaises(OSError):
                        guarded_curl_request(request, object(), 'GET', target,
                                             resolve_option='PIN')
                    dns.assert_not_called()

    def test_offsite_redirect_never_resolved(self):
        calls = []

        def request(session, method, url, **kwargs):
            calls.append(url)
            return Response(302, 'https://attacker.example/secret')

        with patch('browser_http.socket.getaddrinfo', return_value=PUBLIC_DNS) as dns:
            with self.assertRaises(OSError):
                guarded_curl_request(request, object(), 'GET',
                                     'https://vt.tiktok.com/ABC',
                                     resolve_option='PIN')
        self.assertEqual(calls, ['https://vt.tiktok.com/ABC'])
        self.assertEqual(dns.call_count, 1)

    def test_method_and_proxies_rejected(self):
        request = lambda *_a, **_kw: self.fail('Must not connect')
        for kwargs in ({'method': 'POST'},
                       {'method': 'GET', 'proxy': 'socks5://localhost:9050'},
                       {'method': 'GET', 'proxies': {'https': 'http://127.0.0.1:1080'}}):
            method = kwargs.pop('method')
            with self.assertRaises(OSError):
                guarded_curl_request(request, object(), method,
                                     'https://www.tiktok.com/', resolve_option='PIN',
                                     **kwargs)


if __name__ == '__main__':
    unittest.main()
