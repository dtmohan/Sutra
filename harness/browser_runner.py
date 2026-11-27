"""A fixed offline browser probe for one approved, self-contained HTML document."""
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from context_manager import InvalidUpdate
from lifecycle import timeout


MAX_SOURCE_BYTES = 256 * 1024
RUN_SECONDS = 15
_MAX_CDP_BYTES = 2 * 1024 * 1024
_APP = Path('/Applications/Google Chrome.app')
_CHROME = _APP / 'Contents/MacOS/Google Chrome'
_REQUIREMENT = (
    'anchor apple generic and identifier "com.google.Chrome" and '
    'certificate leaf[subject.OU] = "EQHXZ8M8AV"'
)
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; connect-src 'none'; form-action 'none'; object-src 'none'; "
    "frame-src 'none'; worker-src 'none'; base-uri 'none'; sandbox allow-scripts"
)
_ELEMENT_ID = re.compile(r'[A-Za-z][A-Za-z0-9_:-]{0,47}\Z')


class _ProbeFailed(Exception):
    pass


def available():
    return sys.platform == 'darwin' and _CHROME.is_file() and _CHROME.resolve() == _CHROME


def _trusted_browser(seconds):
    if not available():
        return False
    try:
        check = subprocess.run(
            ['/usr/bin/codesign', '--verify', '-R=' + _REQUIREMENT, str(_APP)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={'PATH': os.defpath}, timeout=seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return check.returncode == 0


def _command(profile):
    return [
        str(_CHROME), '--headless=new', '--remote-debugging-pipe', '--enable-automation',
        '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
        '--disable-component-update', '--disable-sync', '--disable-extensions',
        '--disable-crash-reporter', '--disable-breakpad', '--disable-crashpad-for-testing',
        '--disable-gpu', '--disable-webgl', '--renderer-process-limit=2', '--noerrdialogs',
        '--proxy-server=http://sutra-offline.invalid:9', '--proxy-bypass-list=<-loopback>',
        '--host-resolver-rules=MAP * ~NOTFOUND', '--disable-quic',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--js-flags=--max-old-space-size=64 --max-semi-space-size=8',
        '--user-data-dir=' + str(profile), 'about:blank',
    ]


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


class _CDP:
    def __init__(self, out_fd, in_fd, deadline, html):
        self.out_fd = out_fd
        self.in_fd = in_fd
        self.deadline = deadline
        self.html = base64.b64encode(html).decode('ascii')
        self.pending = bytearray()
        self.read_bytes = 0
        self.sequence = 0
        self.target = None
        self.session = None
        self.context = None
        self.approved_url = None
        self.document_url = None
        self.fulfilled = False
        self.blocked = 0
        self.errors = []
        self.error_count = 0
        os.set_blocking(out_fd, False)
        os.set_blocking(in_fd, False)

    def _remaining(self):
        left = self.deadline-time.monotonic()
        if left <= 0:
            raise TimeoutError('Browser probe exceeded its time allocation.')
        return left

    def _send(self, method, params=None, session=None):
        self.sequence += 1
        data = {'id': self.sequence, 'method': method, 'params': params or {}}
        if session:
            data['sessionId'] = session
        pending = memoryview(json.dumps(data, separators=(',', ':')).encode() + b'\0')
        while pending:
            if not select.select([], [self.out_fd], [], self._remaining())[1]:
                raise TimeoutError('Browser probe exceeded its time allocation.')
            try:
                pending = pending[os.write(self.out_fd, pending):]
            except BlockingIOError:
                continue
        return self.sequence

    def _message(self):
        while b'\0' not in self.pending:
            if not select.select([self.in_fd], [], [], self._remaining())[0]:
                raise TimeoutError('Browser probe exceeded its time allocation.')
            chunk = os.read(self.in_fd, 65536)
            if not chunk:
                raise _ProbeFailed('Chrome closed before the browser probe could settle.')
            self.read_bytes += len(chunk)
            if self.read_bytes > _MAX_CDP_BYTES:
                raise _ProbeFailed('The browser event stream reached its byte limit.')
            self.pending.extend(chunk)
        end = self.pending.index(0)
        message = json.loads(self.pending[:end])
        del self.pending[:end + 1]
        return message

    def _request(self, params, session):
        request = params['request']
        identifier = params['requestId']
        if (session == self.session and self.approved_url and request['url'] == self.approved_url
                and request['method'] == 'GET' and params['resourceType'] == 'Document'
                and params.get('frameId') == self.frame):
            self.approved_url = None
            self.fulfilled = True
            self._send('Fetch.fulfillRequest', {
                'requestId': identifier, 'responseCode': 200, 'body': self.html,
                'responseHeaders': [
                    {'name': 'Content-Type', 'value': 'text/html; charset=utf-8'},
                    {'name': 'Content-Security-Policy', 'value': _CSP},
                    {'name': 'Cache-Control', 'value': 'no-store'},
                    {'name': 'Referrer-Policy', 'value': 'no-referrer'},
                    {'name': 'X-Content-Type-Options', 'value': 'nosniff'},
                ],
            }, session)
        else:
            self.blocked += 1
            self._send('Fetch.failRequest', {'requestId': identifier, 'errorReason': 'BlockedByClient'}, session)

    def _event(self, message):
        method = message.get('method')
        params = message.get('params', {})
        session = message.get('sessionId')
        if method == 'Fetch.requestPaused':
            self._request(params, session)
        elif method == 'Target.targetCreated':
            target = params['targetInfo']
            if self.target and target.get('openerId') == self.target:
                self._send('Target.closeTarget', {'targetId': target['targetId']})
        elif method == 'Page.javascriptDialogOpening' and session == self.session:
            self._send('Page.handleJavaScriptDialog', {'accept': False}, session)
        elif method == 'Page.frameNavigated' and session == self.session and self.document_url:
            frame = params['frame']
            if frame['id'] == self.frame and frame['url'] != self.document_url:
                raise _ProbeFailed('The approved page navigated away during the browser probe.')
        elif method == 'Runtime.exceptionThrown' and session == self.session:
            details = params.get('exceptionDetails', {})
            self.error_count += 1
            if len(self.errors) < 4:
                raw = details.get('exception', {}).get('description') or details.get('text', '')
                clean = ' '.join(str(raw).splitlines()[:1]).strip()
                if clean:
                    self.errors.append(_clean(clean, 160))
        elif method == 'Log.entryAdded' and session == self.session:
            entry = params.get('entry', {})
            if entry.get('source') == 'security' and 'Content Security Policy' in entry.get('text', ''):
                self.blocked += 1
        elif method in ('Inspector.targetCrashed', 'Inspector.detached') and session == self.session:
            raise _ProbeFailed('The browser target closed during the probe.')

    def command(self, method, params=None, *, page=False):
        identifier = self._send(method, params, self.session if page else None)
        while True:
            self._remaining()
            message = self._message()
            if message.get('id') == identifier:
                if 'error' in message:
                    raise _ProbeFailed(f'Chrome could not perform {method}.')
                return message.get('result', {})
            if 'method' in message:
                self._event(message)

    def start(self):
        product = self.command('Browser.getVersion').get('product', '')
        if not re.fullmatch(r'Chrome/[0-9]+(?:\.[0-9]+)*', product):
            raise _ProbeFailed('The browser did not report the expected Chrome runtime.')
        browser_context = self.command('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
        self.command('Browser.setDownloadBehavior', {'behavior': 'deny', 'browserContextId': browser_context})
        self.command('Target.setDiscoverTargets', {'discover': True})
        # A new context avoids the default launch tab racing Chrome's startup navigation.
        self.target = self.command('Target.createTarget', {
            'url': 'about:blank', 'browserContextId': browser_context})['targetId']
        self.session = self.command('Target.attachToTarget', {'targetId': self.target, 'flatten': True})['sessionId']
        for method in ('Page.enable', 'Runtime.enable', 'Log.enable', 'Network.enable'):
            self.command(method, page=True)
        self.command('Network.emulateNetworkConditions', {
            'offline': True, 'latency': 0, 'downloadThroughput': 0, 'uploadThroughput': 0}, page=True)
        self.command('Fetch.enable', {'patterns': [{'urlPattern': '*'}]}, page=True)
        self.frame = self.command('Page.getFrameTree', page=True)['frameTree']['frame']['id']
        return product

    def viewport(self, width, height, *, mobile=False):
        self.command('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': mobile,
            'screenWidth': width, 'screenHeight': height}, page=True)

    def load(self):
        url = 'http://sutra-browser.invalid/' + uuid.uuid4().hex + '.html'
        self.approved_url = url
        self.fulfilled = False
        navigation = self.command('Page.navigate', {'url': url}, page=True)
        frame = self.command('Page.getFrameTree', page=True)['frameTree']['frame']
        if 'errorText' in navigation or not self.fulfilled or frame.get('url') != url:
            raise _ProbeFailed('The approved HTML did not load in the offline browser.')
        self.document_url = url
        self.context = self.command('Page.createIsolatedWorld',
                                 {'frameId': self.frame, 'worldName': 'Sutra fixed probe'}, page=True)['executionContextId']
        while self.eval('document.readyState') != 'complete':
            time.sleep(min(.05, self._remaining()))

    def eval(self, expression):
        response = self.command('Runtime.evaluate', {
            'expression': expression, 'contextId': self.context, 'returnByValue': True,
            'silent': True, 'allowUnsafeEvalBlockedByCSP': False}, page=True)
        if 'exceptionDetails' in response:
            raise _ProbeFailed('Could not inspect the loaded browser document.')
        return response.get('result', {}).get('value')


def _clean(value, chars):
    return ' '.join(str(value).split())[:chars]


def _observation(kind, text, *selectors, measurement=None):
    row = {'kind': kind, 'text': text,
           'selectors': list(dict.fromkeys(item for item in selectors
                                          if isinstance(item, str) and _ELEMENT_ID.fullmatch(item)))}
    if measurement is not None:
        row['measurement'] = measurement
    return row


_LOOK = '''(() => {
  const viewport = Number(document.documentElement.clientWidth);
  const canvas = document.querySelector('canvas');
  const canvasRect = canvas && canvas.getBoundingClientRect();
  const buttons = Array.from(document.querySelectorAll('button[id]')).slice(0, 24)
    .filter(button => button.getClientRects().length && !button.disabled && !button.form
      && button.type !== 'reset')
    .map(button => ({id: button.id.slice(0, 49), label: button.innerText.trim().slice(0, 70)}));
  const fences = Array.from(document.querySelectorAll('body, main, div, section')).slice(0, 100)
    .flatMap(el => Array.from(el.childNodes).slice(0, 100))
    .filter(node => node.nodeType === Node.TEXT_NODE && node.data.includes('```')).length;
  return {
    title: document.title.slice(0, 96),
    canvas: canvasRect && {id: canvas.id.slice(0, 49), width: Math.round(canvasRect.width),
      height: Math.round(canvasRect.height), left: Math.round(canvasRect.left),
      right: Math.round(canvasRect.right)},
    viewport,
    buttons,
    fences,
  };
})()'''

_FORM = '''(() => {
  const date = document.querySelector('input[type=date][id]');
  const time = document.querySelector('input[type=time][id]');
  const offset = Array.from(document.querySelectorAll('input[id]')).slice(0, 40)
    .find(input => /utc.?offset|time.?zone.?offset/i.test(input.id)
      || /utc offset/i.test(input.getAttribute('aria-label') || ''));
  const button = Array.from(document.querySelectorAll('button[id]')).slice(0, 30)
    .find(item => /^(calculate|compute)$/i.test(item.innerText.trim()) && !item.form && !item.disabled);
  if (!date || !time || !offset || !button) return null;
  for (let group = button.parentElement, i = 0; group && i < 4; group = group.parentElement, i++) {
    if (!group.contains(date) || !group.contains(time) || !group.contains(offset)) continue;
    const matches = Array.from(group.querySelectorAll('[id]')).slice(0, 40)
      .filter(node => /result|output/i.test(node.id) && !node.matches('input, button'));
    if (matches.length === 1) {
      return {date: date.id, time: time.id, offset: offset.id, button: button.id, result: matches[0].id};
    }
  }
  return null;
})()'''


def _offset_probe(client):
    form = client.eval(_FORM)
    if not isinstance(form, dict) or set(form) != {'date', 'time', 'offset', 'button', 'result'} or any(
            not isinstance(value, str) or not _ELEMENT_ID.fullmatch(value) for value in form.values()):
        return None

    def set_offset(value):
        fields = json.dumps({**form, 'value': value})
        applied = client.eval('''(() => {
          const f = ''' + fields + ''';
          const values = [[f.date, '1990-01-01'], [f.time, '12:00'], [f.offset, f.value]];
          for (const [id, value] of values) {
            const input = document.getElementById(id);
            if (!input) return false;
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
          }
          const button = document.getElementById(f.button);
          if (!button || button.disabled || !values.every(([id, value]) => document.getElementById(id)?.value === value)) return false;
          button.click();
          return true;
        })()''')
        if not applied:
            return None
        time.sleep(min(.07, client._remaining()))
        return client.eval('''(() => {
          const text = document.getElementById(''' + json.dumps(form['result']) + ''')?.innerText.trim();
          return text && text.length <= 1000 ? text : null;
        })()''')

    before, after = set_offset('+00:00'), set_offset('+05:30')
    if before and after:
        result = 'identical' if before == after else 'different'
        return _observation('interaction',
            'The same synthetic birth date and time produced ' + result +
            ' visible results with UTC offsets +00:00 and +05:30.',
            form['offset'], form['button'], form['result'], measurement={
                'probe': 'offset_comparison', 'outputs': result, 'control': form['offset'],
                'action': form['button'], 'result': form['result'],
            })
    return None


_DISPLAY_BUTTONS = re.compile(r'\b(tropical|sidereal|heliocentric|geocentric|gear back|'
                              r'front face|dark mode|light mode|theme|reset|pause)\b', re.I)


def _display_probes(client, buttons):
    facts = []
    for button in buttons:
        identifier, label = button['id'], _clean(button['label'], 55)
        if not _ELEMENT_ID.fullmatch(identifier) or not _DISPLAY_BUTTONS.search(label):
            continue
        argument = json.dumps(identifier)
        outcome = client.eval('''(() => {
          const button = document.getElementById(''' + argument + ''');
          if (!button || button.disabled || button.form) return null;
          const before = button.className + ' ' + button.innerText;
          button.click();
          return {changed: before !== button.className + ' ' + button.innerText};
        })()''')
        if outcome and outcome['changed']:
            facts.append(_observation('interaction', f'Clicking “{label}” changed its displayed label or state.', identifier))
        if len(facts) >= 3:
            break
    return facts


def _probe(client):
    engine = client.start()
    client.viewport(1280, 900)
    client.load()
    desktop = client.eval(_LOOK)
    canvas = desktop['canvas']
    size = f' with a {canvas["width"]} × {canvas["height"]} CSS px canvas' if canvas else ''
    title = _clean(desktop['title'], 96)
    facts = [_observation('load', f'Chrome loaded the approved page “{title}”{size}.',
                          canvas['id'] if canvas else None)]
    if desktop['fences']:
        facts.append(_observation('layout', 'The rendered document contains literal Markdown fence text.',
                                  measurement={'probe': 'visible_placeholder'}))
    facts.extend(_display_probes(client, desktop['buttons']))
    offset = _offset_probe(client)
    if offset:
        facts.append(offset)
    client.viewport(390, 844, mobile=True)
    mobile = client.eval(_LOOK)
    canvas = mobile['canvas']
    if canvas and (canvas['left'] < 0 or canvas['right'] > mobile['viewport']):
        facts.append(_observation('layout',
            f'At a 390 px viewport the {canvas["width"]} px canvas extended outside the '
            f'{mobile["viewport"]} px layout viewport (left {canvas["left"]}, right {canvas["right"]}).',
            canvas['id'], measurement={'probe': 'canvas_overflow', 'viewport': 390,
                'layout_width': mobile['viewport'], 'canvas_width': canvas['width'],
                'left': canvas['left'], 'right': canvas['right']}))
    if client.error_count:
        facts.extend(_observation('console', 'Browser observed an uncaught JavaScript exception: ' + item,
                                  measurement={'probe': 'script_exception'})
                     for item in client.errors)
    else:
        facts.append(_observation('console', 'No uncaught JavaScript exceptions were observed during these probes.'))
    if client.blocked:
        facts.append(_observation('network', 'The offline browser blocked one or more requests or policy violations.'))
    return engine, facts


def run(html):
    """Load approved bytes in a temporary Chrome profile and return observations."""
    if not isinstance(html, bytes) or not len(html) <= MAX_SOURCE_BYTES or not html:
        raise InvalidUpdate('Browser probe requires one nonempty HTML document of at most 256 KiB.')
    try:
        html.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise InvalidUpdate('Browser probe requires UTF-8 HTML.') from exc

    started = time.monotonic()
    deadline = started + timeout(RUN_SECONDS)
    result = {'status': 'unavailable', 'engine': None, 'observations': [],
              'diagnostic': 'A trusted installation of Google Chrome is required on macOS.'}
    if _trusted_browser(min(3, deadline-time.monotonic())):
        try:
            result = _run_isolated(html, deadline)
        except TimeoutError:
            result.update(status='timeout', diagnostic='Chrome did not finish within the browser probe allocation.')
        except _ProbeFailed as exc:
            result.update(status='failed', diagnostic=str(exc))
        except (OSError, ValueError, TypeError, KeyError):
            result.update(status='failed', diagnostic='The isolated Chrome probe could not be completed.')
    return dict(result, elapsed_seconds=round(time.monotonic()-started, 3))


def _run_isolated(html, deadline):
    import fcntl
    with tempfile.TemporaryDirectory(prefix='sutra-browser-') as temp, ExitStack() as files:
        private = Path(temp).resolve()
        with ExitStack() as launch:
            incoming, to_chrome = os.pipe()
            launch.callback(os.close, incoming)
            files.callback(os.close, to_chrome)
            from_chrome, outgoing = os.pipe()
            launch.callback(os.close, outgoing)
            files.callback(os.close, from_chrome)
            null = os.open(os.devnull, os.O_RDWR)
            launch.callback(os.close, null)
            # Duplicate above 4 before assigning Chromium's fixed pipe descriptors.
            read_copy = fcntl.fcntl(incoming, fcntl.F_DUPFD_CLOEXEC, 10)
            launch.callback(os.close, read_copy)
            write_copy = fcntl.fcntl(outgoing, fcntl.F_DUPFD_CLOEXEC, 10)
            launch.callback(os.close, write_copy)
            env = {'HOME': str(private), 'TMPDIR': str(private), 'PATH': os.defpath, 'LANG': 'en_US.UTF-8'}
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGALRM})
            try:
                pid = os.posix_spawn(str(_CHROME), _command(private / 'profile'), env, setpgroup=0,
                    setsigmask=previous, file_actions=[
                        (os.POSIX_SPAWN_DUP2, null, 0), (os.POSIX_SPAWN_DUP2, null, 1), (os.POSIX_SPAWN_DUP2, null, 2),
                        (os.POSIX_SPAWN_DUP2, read_copy, 3), (os.POSIX_SPAWN_DUP2, write_copy, 4),
                    ])
                files.callback(_kill_group, pid)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        client = _CDP(to_chrome, from_chrome, deadline, html)
        engine, observations = _probe(client)
        return {'status': 'observed', 'engine': engine, 'observations': observations, 'diagnostic': None,
                'observation_profile': 'browser-probes-v2'}
