import os
import re
import sys
import json
import time
import shutil
import socket
import select
import platform
import tempfile
import unittest
import subprocess
from multiprocessing import Process

class TestUnit(unittest.TestCase):

    pardir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    architecture = platform.architecture()[0]
    maxDiff = None

    def setUp(self):
        self._run()

    def tearDown(self):
        self.stop()

        with open(self.testdir + '/unit.log', 'r', encoding='utf-8',
            errors='ignore') as f:
            self._check_alerts(f.read())

        if '--leave' not in sys.argv:
            shutil.rmtree(self.testdir)

    def check_modules(self, *modules):
        self._run()

        for i in range(50):
            with open(self.testdir + '/unit.log', 'r') as f:
                log = f.read()
                m = re.search('controller started', log)

                if m is None:
                    time.sleep(0.1)
                else:
                    break

        if m is None:
            self.stop()
            exit("Unit is writing log too long")

        current_dir = os.path.dirname(os.path.abspath(__file__))

        missed_module = ''
        for module in modules:
            if module == 'go':
                env = os.environ.copy()
                env['GOPATH'] = self.pardir + '/go'

                try:
                    process = subprocess.Popen(['go', 'build', '-o',
                        self.testdir + '/go/check_module',
                        current_dir + '/go/empty/app.go'], env=env)
                    process.communicate()

                    m = module if process.returncode == 0 else None

                except:
                    m = None

            else:
                m = re.search('module: ' + module, log)

            if m is None:
                missed_module = module
                break

        self.stop()
        self._check_alerts(log)
        shutil.rmtree(self.testdir)

        if missed_module:
            raise unittest.SkipTest('Unit has no ' + missed_module + ' module')

    def stop(self):
        if self._started:
            self._stop()

    def _run(self):
        self.testdir = tempfile.mkdtemp(prefix='unit-test-')

        os.mkdir(self.testdir + '/state')

        print()

        def _run_unit():
            subprocess.call([self.pardir + '/build/unitd',
                '--no-daemon',
                '--modules', self.pardir + '/build',
                '--state', self.testdir + '/state',
                '--pid', self.testdir + '/unit.pid',
                '--log', self.testdir + '/unit.log',
                '--control', 'unix:' + self.testdir + '/control.unit.sock'])

        self._p = Process(target=_run_unit)
        self._p.start()

        if not self.waitforfiles(self.testdir + '/unit.pid',
            self.testdir + '/unit.log', self.testdir + '/control.unit.sock'):
            exit("Could not start unit")

        self._started = True

        self.skip_alerts = [r'read signalfd\(4\) failed']
        self.skip_sanitizer = False

    def _stop(self):
        with open(self.testdir + '/unit.pid', 'r') as f:
            pid = f.read().rstrip()

        subprocess.call(['kill', '-s', 'QUIT', pid])

        for i in range(50):
            if not os.path.exists(self.testdir + '/unit.pid'):
                break
            time.sleep(0.1)

        if os.path.exists(self.testdir + '/unit.pid'):
            exit("Could not terminate unit")

        self._started = False

        self._p.join(timeout=1)
        self._terminate_process(self._p)

    def _terminate_process(self, process):
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

            if process.is_alive():
                exit("Could not terminate process " + process.pid)

        if process.exitcode:
            exit("Child process terminated with code " + str(process.exitcode))

    def _check_alerts(self, log):
        found = False

        alerts = re.findall('.+\[alert\].+', log)

        if alerts:
            print('All alerts/sanitizer errors found in log:')
            [print(alert) for alert in alerts]
            found = True

        if self.skip_alerts:
            for skip in self.skip_alerts:
                alerts = [al for al in alerts if re.search(skip, al) is None]

        self.assertFalse(alerts, 'alert(s)')

        if not self.skip_sanitizer:
            self.assertFalse(re.findall('.+Sanitizer.+', log),
                'sanitizer error(s)')

        if found:
            print('skipped.')

    def waitforfiles(self, *files):
        for i in range(50):
            wait = False
            ret = False

            for f in files:
                if not os.path.exists(f):
                   wait = True
                   break

            if wait:
                time.sleep(0.1)

            else:
                ret = True
                break

        return ret

class TestUnitHTTP(TestUnit):

    def http(self, start_str, **kwargs):
        sock_type = 'ipv4' if 'sock_type' not in kwargs else kwargs['sock_type']
        port = 7080 if 'port' not in kwargs else kwargs['port']
        url = '/' if 'url' not in kwargs else kwargs['url']
        http = 'HTTP/1.0' if 'http_10' in kwargs else 'HTTP/1.1'

        headers = ({
            'Host': 'localhost',
            'Connection': 'close'
        } if 'headers' not in kwargs else kwargs['headers'])

        body = b'' if 'body' not in kwargs else kwargs['body']
        crlf = '\r\n'

        if 'addr' not in kwargs:
            addr = '::1' if sock_type == 'ipv6' else '127.0.0.1'
        else:
            addr = kwargs['addr']

        sock_types = {
            'ipv4': socket.AF_INET,
            'ipv6': socket.AF_INET6,
            'unix': socket.AF_UNIX
        }

        if 'sock' not in kwargs:
            sock = socket.socket(sock_types[sock_type], socket.SOCK_STREAM)

            connect_args = addr if sock_type == 'unix' else (addr, port)
            try:
                sock.connect(connect_args)
            except ConnectionRefusedError:
                sock.close()
                return None

        else:
            sock = kwargs['sock']

        sock.setblocking(False)

        if 'raw' not in kwargs:
            req = ' '.join([start_str, url, http]) + crlf

            if body is not b'':
                if isinstance(body, str):
                    body = body.encode()

                if 'Content-Length' not in headers:
                    headers['Content-Length'] = len(body)

            for header, value in headers.items():
                req += header + ': ' + str(value) + crlf

            req = (req + crlf).encode() + body

        else:
            req = start_str

        sock.sendall(req)

        if '--verbose' in sys.argv:
            print('>>>', req, sep='\n')

        resp = self.recvall(sock)

        if '--verbose' in sys.argv:
            print('<<<', resp.encode('utf-8'), sep='\n')

        if 'raw_resp' not in kwargs:
            resp = self._resp_to_dict(resp)

        if 'start' not in kwargs:
            sock.close()
            return resp

        return (resp, sock)

    def delete(self, **kwargs):
        return self.http('DELETE', **kwargs)

    def get(self, **kwargs):
        return self.http('GET', **kwargs)

    def post(self, **kwargs):
        return self.http('POST', **kwargs)

    def put(self, **kwargs):
        return self.http('PUT', **kwargs)

    def recvall(self, sock, buff_size=4096):
        data = b''
        while select.select([sock], [], [], 1)[0]:
            try:
                part = sock.recv(buff_size)
            except:
                break

            data += part

            if not len(part):
                break

        return data.decode()

    def _resp_to_dict(self, resp):
        m = re.search('(.*?\x0d\x0a?)\x0d\x0a?(.*)', resp, re.M | re.S)

        if not m:
            return {}

        headers_text, body = m.group(1), m.group(2)

        p = re.compile('(.*?)\x0d\x0a?', re.M | re.S)
        headers_lines = p.findall(headers_text)

        status = re.search('^HTTP\/\d\.\d\s(\d+)|$', headers_lines.pop(0)).group(1)

        headers = {}
        for line in headers_lines:
            m = re.search('(.*)\:\s(.*)', line)

            if m.group(1) not in headers:
                headers[m.group(1)] = m.group(2)
            elif isinstance(headers[m.group(1)], list):
                headers[m.group(1)].append(m.group(2))
            else:
                headers[m.group(1)] = [headers[m.group(1)], m.group(2)]

        return {
            'status': int(status),
            'headers': headers,
            'body': body
        }

class TestUnitControl(TestUnitHTTP):

    # TODO socket reuse
    # TODO http client

    def conf(self, conf, path='/'):
        if isinstance(conf, dict):
            conf = json.dumps(conf)

        return json.loads(self.put(
            url=path,
            body=conf,
            sock_type='unix',
            addr=self.testdir + '/control.unit.sock'
        )['body'])

    def conf_get(self, path='/'):
        return json.loads(self.get(
            url=path,
            sock_type='unix',
            addr=self.testdir + '/control.unit.sock'
        )['body'])

    def conf_delete(self, path='/'):
        return json.loads(self.delete(
            url=path,
            sock_type='unix',
            addr=self.testdir + '/control.unit.sock'
        )['body'])

class TestUnitApplicationProto(TestUnitControl):

    current_dir = os.path.dirname(os.path.abspath(__file__))

    def sec_epoch(self):
        return time.mktime(time.gmtime())

    def date_to_sec_epoch(self, date):
        return time.mktime(time.strptime(date, '%a, %d %b %Y %H:%M:%S %Z'))

    def search_in_log(self, pattern):
        with open(self.testdir + '/unit.log', 'r') as f:
            return re.search(pattern, f.read())

class TestUnitApplicationPython(TestUnitApplicationProto):
    def load(self, script, name=None):
        if name is None:
            name = script

        self.conf({
            "listeners": {
                "*:7080": {
                    "application": name
                }
            },
            "applications": {
                name: {
                    "type": "python",
                    "processes": { "spare": 0 },
                    "path": self.current_dir + '/python/' + script,
                    "working_directory": self.current_dir + '/python/' + script,
                    "module": "wsgi"
                }
            }
        })

class TestUnitApplicationRuby(TestUnitApplicationProto):
    def load(self, script, name='config.ru'):
        self.conf({
            "listeners": {
                "*:7080": {
                    "application": script
                }
            },
            "applications": {
                script: {
                    "type": "ruby",
                    "processes": { "spare": 0 },
                    "working_directory": self.current_dir + '/ruby/' + script,
                    "script": self.current_dir + '/ruby/' + script + '/' + name
                }
            }
        })

class TestUnitApplicationPHP(TestUnitApplicationProto):
    def load(self, script, name='index.php'):
        self.conf({
            "listeners": {
                "*:7080": {
                    "application": script
                }
            },
            "applications": {
                script: {
                    "type": "php",
                    "processes": { "spare": 0 },
                    "root": self.current_dir + '/php/' + script,
                    "working_directory": self.current_dir + '/php/' + script,
                    "index": name
                }
            }
        })

class TestUnitApplicationGo(TestUnitApplicationProto):
    def load(self, script, name='app'):

        if not os.path.isdir(self.testdir + '/go'):
            os.mkdir(self.testdir + '/go')

        env = os.environ.copy()
        env['GOPATH'] = self.pardir + '/go'
        process = subprocess.Popen(['go', 'build', '-o',
            self.testdir + '/go/' + name,
            self.current_dir + '/go/' + script + '/' + name + '.go'],
            env=env)
        process.communicate()

        self.conf({
            "listeners": {
                "*:7080": {
                    "application": script
                }
            },
            "applications": {
                script: {
                    "type": "go",
                    "processes": { "spare": 0 },
                    "working_directory": self.current_dir + '/go/' + script,
                    "executable": self.testdir + '/go/' + name
                }
            }
        })

class TestUnitApplicationPerl(TestUnitApplicationProto):
    def load(self, script, name='psgi.pl'):
        self.conf({
            "listeners": {
                "*:7080": {
                    "application": script
                }
            },
            "applications": {
                script: {
                    "type": "perl",
                    "processes": { "spare": 0 },
                    "working_directory": self.current_dir + '/perl/' + script,
                    "script": self.current_dir + '/perl/' + script + '/' + name
                }
            }
        })
