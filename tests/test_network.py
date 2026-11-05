#!/usr/bin/env python3
"""Offline regression tests; no public speed-test traffic or root access needed."""
import http.server
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / 'bin/speed-audit-ru'
INSTALLER = ROOT / 'install.sh'


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/servers':
            content = json.dumps([{'id': 1, 'name': 'Fixture', 'server': self.server.base,
                                   'pingURL': 'empty', 'dlURL': 'large', 'ulURL': 'empty'}]).encode()
            status, mime = 200, 'application/json'
        elif self.path == '/empty':
            content, status, mime = b'', 200, 'application/octet-stream'
        else:
            content = b'x' * (1024 if self.path == '/small' else 6 * 1024 * 1024)
            status = 404 if self.path == '/missing' else 200
            mime = 'text/html' if self.path == '/html' else 'application/octet-stream'
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.base = cls.server.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env['PATH'] = str(self.path) + os.pathsep + self.env['PATH']
        self.env['SPEED_AUDIT_QUIET'] = '1'
        for name, body in {
            'librespeed-cli': '#!/bin/sh\nif [ "$1" = --version ]; then echo "librespeed-cli v1.0.13"; else echo null; echo "fixture timeout" >&2; fi\n',
            'speedtest': '#!/bin/sh\necho OOKLA_MUST_NOT_RUN >&2\nexit 99\n',
        }.items():
            f = self.path / name
            f.write_text(body)
            f.chmod(0o755)

    def bash(self, body, source=AUDIT, expected=0, bounded_stub=False):
        stub = 'run_network_command() { shift; "$@"; };' if bounded_stub else ''
        result = subprocess.run(['bash', '-c', f'source {shlex.quote(str(source))}; {stub}\n{body}'],
                                cwd=self.path, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn('OOKLA_MUST_NOT_RUN', result.stdout + result.stderr)
        return result.stdout

    def fixture(self, name, value):
        p = self.path / name
        p.write_text(json.dumps(value))
        return shlex.quote(str(p))

    def test_reject_failed_measurements(self):
        invalid = [None, [], {}, {'download': 0, 'upload': 0},
                   {'download': -1, 'upload': 10}, {'download': '100', 'upload': 20},
                   {'download': 100}, {'download': 100, 'upload': 20, 'error': 'failed'},
                   [{'download': 1, 'upload': 1}, {'download': 2, 'upload': 2}]]
        for i, value in enumerate(invalid):
            with self.subTest(value=value):
                path = self.fixture(f'invalid-{i}.json', value)
                self.bash(f'reset_network_result; if parse_librespeed_result {path} Test; then exit 9; fi; [[ "$NETWORK_TEST_OK" == 0 ]]')
        p = self.path / 'broken.json'
        p.write_text('{invalid')
        self.bash(f'if parse_librespeed_result {shlex.quote(str(p))} Test; then exit 9; fi')

    def test_peak_download_and_upload_keep_their_origins(self):
        paths = [self.fixture(f'{i}.json', {'download': d, 'upload': u, 'ping': ping, 'jitter': 1})
                 for i, (d, u, ping) in enumerate([(100, 200, 10), (300, 50, 20), (200, 400, 30)])]
        calls = '; '.join(f'parse_librespeed_result {p} Server{i}' for i, p in enumerate(paths))
        result = self.bash('reset_network_result; ' + calls + '; printf "%s|%s|%s|%s|%s|%s" "$NETWORK_TEST_DOWNLOAD" "$NETWORK_TEST_UPLOAD" "$NETWORK_TEST_SERVER" "$NETWORK_TEST_UPLOAD_SERVER" "$NETWORK_TEST_PING" "${#NETWORK_SAMPLES[@]}"')
        self.assertEqual(result, '300|400|Server1|Server2|20|3')

    def test_json_mbps_are_not_guessed_or_rescaled(self):
        path = self.fixture('valid.json', [{'download': 2000000, 'upload': 123.45}])
        result = self.bash(f'parse_librespeed_result {path} Test; printf "%s|%s|%s" "$NETWORK_TEST_DOWNLOAD" "$NETWORK_TEST_PING" "$NETWORK_TEST_JITTER"')
        self.assertEqual(result, '2000000||')

    def test_null_exit_zero_falls_back_to_three_real_http_downloads(self):
        # Use the actual GNU timeout when present (Ubuntu); otherwise stub only that wrapper.
        body = f'''
CONFIG_FILE=/nonexistent
SPEED_AUDIT_LIBRESPEED_SERVERS_URL={shlex.quote(self.base + '/servers')}
SPEED_AUDIT_HTTP_PROBE_URLS={shlex.quote(self.base + '/large')}
load_runtime_config
probe_host_latency() {{ return 1; }}
command_exists() {{ if [[ "$1" == timeout ]]; then return 0; fi; command -v "$1" >/dev/null; }}
run_best_network_test
[[ "$NETWORK_TEST_LIMITED" == 1 && -z "$NETWORK_TEST_UPLOAD" && -z "$NETWORK_TEST_PING" ]]
[[ "${{#NETWORK_SAMPLES[@]}}" == 3 ]]
[[ "${{NETWORK_ATTEMPTS[0]}}" == *"fixture timeout"* ]]
print_network_result
print_overall_assessment 1
'''
        out = self.bash(body, bounded_stub=shutil.which("timeout") is None)
        self.assertIn('Отдача: нет данных', out)
        self.assertIn('Проверка сети частичная', out)
        self.assertNotIn('Все в порядке, сервер можно использовать', out)

    def test_three_full_measurements_repeat_a_reachable_server(self):
        counter = self.path / 'counter'
        cli = self.path / 'librespeed-cli'
        cli.write_text('#!/usr/bin/env python3\nimport json\nfrom pathlib import Path\np=Path(' + repr(str(counter)) + ')\ni=int(p.read_text()) if p.exists() else 0\np.write_text(str(i+1))\nd,u,ping=[(100,200,10),(300,50,20),(200,400,30)][i]\nprint(json.dumps([{"download":d,"upload":u,"ping":ping,"jitter":1}]))\n')
        cli.chmod(0o755)
        body = f'''CONFIG_FILE=/nonexistent
SPEED_AUDIT_LIBRESPEED_SERVERS_URL={shlex.quote(self.base + '/servers')}
SPEED_AUDIT_HTTP_PROBE_URLS={shlex.quote(self.base + '/missing')}
load_runtime_config
command_exists() {{ if [[ "$1" == timeout ]]; then return 0; fi; command -v "$1" >/dev/null; }}
run_best_network_test
[[ "${{#NETWORK_SAMPLES[@]}}" == 3 && "$NETWORK_TEST_DOWNLOAD" == 300 && "$NETWORK_TEST_UPLOAD" == 400 ]]
[[ "$NETWORK_TEST_LIMITED" == 0 && "$NETWORK_TEST_PING" == 20 && "${{#NETWORK_ATTEMPTS[@]}}" == 0 ]]
print_overall_assessment 0
'''
        out = self.bash(body, bounded_stub=shutil.which('timeout') is None)
        self.assertIn('Все в порядке', out)
        self.assertEqual(counter.read_text(), '3')

    def test_http_errors_small_files_and_html_are_not_speed_results(self):
        for route in ['/missing', '/small', '/html']:
            with self.subTest(route=route):
                body = f'NETWORK_DEADLINE=$((SECONDS + 20)); HTTP_PROBE_URLS={shlex.quote(self.base + route)}; reset_network_result; if run_http_probe_test {shlex.quote(self.temp.name)}; then exit 9; fi; [[ "$NETWORK_TEST_OK" == 0 ]]'
                self.bash(body, bounded_stub=True)

    def test_partial_timed_http_transfer_can_be_a_valid_sample(self):
        body = r'''NETWORK_DEADLINE=$((SECONDS + 20)); HTTP_PROBE_URLS=http://fixture/large
curl() { printf '206|10485760|10000000|9.001|application/octet-stream'; return 28; }
reset_network_result
run_http_probe_test .
[[ "${#NETWORK_SAMPLES[@]}" == 3 && "$NETWORK_TEST_DOWNLOAD" == 80.000000000 ]]
'''
        self.bash(body, bounded_stub=True)

    @unittest.skipUnless(shutil.which('timeout'), 'GNU timeout is required for this Linux check')
    def test_gnu_timeout_interrupts_a_stalled_command(self):
        self.bash('NETWORK_DEADLINE=$((SECONDS + 1)); if run_network_command 10 bash -c "sleep 5"; then exit 9; fi; (( SECONDS < 4 ))')

    def test_total_failure_does_not_report_zero_or_healthy(self):
        out = self.bash('reset_network_result; print_overall_assessment 2')
        self.assertIn('скорость сети измерить не удалось', out)
        self.assertNotIn('Все в порядке', out)

    def test_incomplete_sample_count_is_disclosed(self):
        out = self.bash('reset_network_result; record_network_sample LibreSpeed Test 100 100 10 1 ""; print_overall_assessment 0')
        self.assertIn('успешных замеров 1 из 3', out)
        self.assertNotIn('Все в порядке', out)

    def test_invalid_config_limits_use_bounded_defaults(self):
        out = self.bash('CONFIG_FILE=/nonexistent; SPEED_AUDIT_NET_SAMPLES=999999; SPEED_AUDIT_NET_TIMEOUT=0; SPEED_AUDIT_NET_DURATION=bad; load_runtime_config; printf "%s|%s|%s" "$NETWORK_SAMPLE_TARGET" "$NETWORK_BUDGET" "$NETWORK_DURATION"')
        self.assertEqual(out, '3|180|8')

    def test_removed_ids_are_filtered_and_live_ids_discovered(self):
        servers = self.fixture('servers.json', [{'id': 79, 'name': 'Prague'}, {'id': 101, 'name': 'Helsinki'}])
        out = self.bash(f'LIBRESPEED_SERVER_IDS=102,79; librespeed_candidate_ids {servers} | awk \'!seen[$0]++\'')
        self.assertEqual(out.splitlines(), ['79', '101'])

    def test_old_default_http_config_gets_new_ru_endpoints(self):
        old = 'https://mirror.yandex.ru/ubuntu/ls-lR.gz https://proof.ovh.net/files/100Mb.dat https://cachefly.cachefly.net/100mb.test https://download.docker.com/linux/ubuntu/dists/jammy/Release'
        out = self.bash(f'CONFIG_FILE=/nonexistent; SPEED_AUDIT_HTTP_PROBE_URLS={shlex.quote(old)}; load_runtime_config; printf "%s" "$HTTP_PROBE_URLS"')
        self.assertIn('https://speedtest.selectel.ru/100MB', out)
        self.assertNotIn('download.docker.com', out)

    def test_budget_prevents_new_network_calls(self):
        # This returns before reaching timeout, so it works on macOS too.
        out = self.bash('NETWORK_DEADLINE=$SECONDS; if run_network_command 5 echo SHOULD_NOT_RUN; then exit 9; fi')
        self.assertNotIn('SHOULD_NOT_RUN', out)

    def test_installer_detects_and_replaces_old_version(self):
        target = self.path / 'audit'
        target.write_text('#!/bin/bash\nAPP_VERSION="1.3.0"\n')
        target.chmod(0o755)
        body = f'''
AUDIT_TARGET={shlex.quote(str(target))}; BIN_TARGET="$AUDIT_TARGET"
command_exists() {{ return 0; }}
if runtime_ready; then exit 9; fi
SPEED_AUDIT_LOCAL_SCRIPT_SOURCE={shlex.quote(str(AUDIT))}
download_audit_script "$AUDIT_TARGET"
runtime_ready
'''
        self.bash(body, source=INSTALLER)
        self.assertIn('APP_VERSION="1.4.0"', target.read_text())

    def test_interrupted_or_invalid_update_keeps_existing_script(self):
        target = self.path / 'audit'
        original = '#!/bin/bash\nAPP_VERSION="1.3.0"\n'
        for mode in ['timeout', 'invalid']:
            with self.subTest(mode=mode):
                target.write_text(original)
                if mode == 'timeout':
                    fake_curl = 'curl() { while (($#)); do if [[ "$1" == -o ]]; then shift; printf partial > "$1"; break; fi; shift; done; return 28; }'
                else:
                    fake_curl = 'curl() { while (($#)); do if [[ "$1" == -o ]]; then shift; printf "not a valid release" > "$1"; break; fi; shift; done; }'
                self.bash(f'resolve_local_script_source() {{ return 1; }}; {fake_curl}; if download_audit_script {shlex.quote(str(target))}; then exit 9; fi', source=INSTALLER)
                self.assertEqual(target.read_text(), original)
                self.assertFalse(list(self.path.glob('.speed-audit-ru.*')))

    def test_existing_config_is_preserved(self):
        config = self.path / 'existing.conf'
        config.write_text('CUSTOM_SETTING=keep\n')
        self.bash(f'CONFIG_FILE={shlex.quote(str(config))}; CONFIG_UPDATE=0; write_runtime_config', source=INSTALLER)
        self.assertEqual(config.read_text(), 'CUSTOM_SETTING=keep\n')

    def test_actual_piped_installer_entrypoint(self):
        result = subprocess.run(['bash', '-s', '--', '--help'], input=INSTALLER.read_text(),
                                env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Использование:', result.stdout)
        self.assertIn('--samples=1..5', result.stdout)

    def test_piped_installer_without_tty_skips_menu(self):
        target = self.path / 'run-audit'
        target.write_text('#!/bin/bash\nprintf "skip=%s" "$SPEED_AUDIT_SKIP_MENU"\n')
        target.chmod(0o755)
        out = self.bash(f'BIN_TARGET={shlex.quote(str(target))}; run_check', source=INSTALLER)
        self.assertEqual(out, 'skip=1')

    def test_blank_enter_exits_menu(self):
        master, slave = pty.openpty()
        process = subprocess.Popen(['bash', '-c', f'source {shlex.quote(str(AUDIT))}; show_action_menu 2 0.02 18 45 435'],
                                   stdin=slave, stdout=slave, stderr=slave, env=self.env)
        os.close(slave)
        data = b''
        try:
            deadline = time.monotonic() + 5
            while 'Введите номер действия:'.encode() not in data and time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    data += os.read(master, 65536)
            self.assertIn('Введите номер действия:'.encode(), data)
            os.write(master, b'\n')
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertNotIn('Нужно ввести'.encode(), data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == '__main__':
    unittest.main(verbosity=2)
