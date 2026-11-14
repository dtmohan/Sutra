const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'extension.js'), 'utf8');
const manifestPath = path.join(root, 'runtime-integrity.json');
const kernel = 'harness/stp_canonical/kernel.py';

function activate({changed, removeKernel = false} = {}) {
  const loaded = [];
  const fixture = {exports: {}};
  const checkedFs = {...fs, readFileSync(file, options) {
    const data = fs.readFileSync(file, options);
    if (removeKernel && file === manifestPath) {
      const manifest = JSON.parse(data);
      delete manifest.sha256[kernel];
      return JSON.stringify(manifest);
    }
    if (file === path.join(root, changed || '<no file>')) {
      return Buffer.concat([Buffer.from(data), Buffer.from('\nchanged')]);
    }
    return data;
  }};
  vm.runInNewContext(source, {module: fixture, exports: fixture.exports, require(name) {
    if (name === 'vscode') return {};
    if (name === 'node:fs') return checkedFs;
    if (name.startsWith('./')) {
      loaded.push(name);
      throw new Error('LOCAL_MODULE_LOAD_REACHED');
    }
    return require(name);
  }});
  let error;
  try { fixture.exports.activate({extensionPath: root}); }
  catch (e) { error = e; }
  return {loaded, error};
}

const good = activate();
assert.match(good.error.message, /LOCAL_MODULE_LOAD_REACHED/);
assert.equal(good.loaded.length, 1);
for (const changed of [kernel, 'harness/host_actions.py', 'extension.js', 'native_provider.js',
                       'media/panel.js', 'bin/telemetry']) {
  const result = activate({changed});
  assert.match(result.error.message, /integrity check failed/, changed);
  assert.deepEqual(result.loaded, [], 'Local modules loaded before integrity validation');
}
assert.match(activate({removeKernel: true}).error.message, /kernel integrity metadata/);
console.log('Runtime integrity: changed application, webview, helper and kernel files blocked before local module loading');
