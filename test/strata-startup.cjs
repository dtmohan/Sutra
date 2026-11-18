'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const vm = require('node:vm');
const startup = require('../strata_startup');

const user = {
  models: {high:'strata:high'},
  'strata.startOnStartup': true,
  'strata.port': 19483,
  'strata.model': 'flash'
};
const config = (global = user, workspace = {}) => () => ({
  inspect: key => ({globalValue:global[key], workspaceValue:workspace[key], workspaceFolderValue:workspace[key]})
});
const mac = {platform:'darwin', uid:501};
const record = {port:user['strata.port'], model:user['strata.model']};
const irrelevant = {
  ...mac,
  probe: () => assert.fail('This configuration should not probe a port'),
  readProfile: () => assert.fail('This configuration should not read a startup profile'),
  launch: () => assert.fail('This configuration should not start a process')
};

async function verify() {
  assert.equal((await startup.start(config({}, user), irrelevant)).status, 'disabled');
  assert.equal((await startup.start(config({...user, models:{high:'ollama-model'}}, user), irrelevant)).status, 'disabled');
  assert.equal((await startup.start(config({...user, 'strata.port':0}, user), irrelevant)).status, 'invalid');
  assert.equal((await startup.start(config(), {...irrelevant, remote:true})).status, 'unsupported');
  assert.equal((await startup.start(config(), {...irrelevant, platform:'linux'})).status, 'unsupported');

  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const settings = {...user, 'strata.port': server.address().port};
    const blocked = await startup.start(config(settings), {
      ...mac,
      readProfile: () => { throw new Error('No startup profile for a manually hosted server'); },
      launch: irrelevant.launch
    });
    assert.equal(blocked.status, 'occupied');
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
  const uncertain = await startup.start(config(), {
    ...mac,
    probe: () => Promise.reject(new Error('Loopback connection timed out')),
    launch: irrelevant.launch
  });
  assert.equal(uncertain.status, 'unavailable');

  const offline = {...mac, probe: () => false, launch: irrelevant.launch};
  const missing = await startup.start(config(), {
    ...offline,
    readProfile: () => { throw new Error('No installed profile'); }
  });
  assert.equal(missing.status, 'setup');
  assert.match(missing.message, /startup\.json/);
  for (const wrong of [{...record, port:1}, {...record, model:'other-model'}]) {
    const mismatch = await startup.start(config(), {...offline, readProfile: () => wrong});
    assert.equal(mismatch.status, 'mismatch');
  }

  const mutable = {...user};
  const revoked = await startup.start(config(mutable), {
    ...offline,
    readProfile: () => {
      mutable['strata.startOnStartup'] = false;
      return record;
    }
  });
  assert.equal(revoked.status, 'changed');

  const calls = [];
  const subject = {exports:{}};
  const source = fs.readFileSync(path.join(__dirname, '../strata_startup.js'), 'utf8');
  vm.runInNewContext(source, {
    module: subject,
    require: name => name === 'node:child_process' ? {
      execFile(command, args, options, done) {
        calls.push({command, args: Array.from(args), shell: options.shell});
        done();
      }
    } : require(name)
  });
  const opts = {
    ...mac,
    readProfile: async () => record,
    probe: async port => {
      assert.equal(port, user['strata.port']);
      return false;
    }
  };
  const results = await Promise.all([
    subject.exports.start(config(), opts),
    subject.exports.start(config(), opts)
  ]);
  assert(results.every(result => result.status === 'requested'));
  const kick = {command:'/bin/launchctl', args:['kickstart', 'gui/501/io.sutra.strata-high'], shell:undefined};
  assert.deepEqual(calls, [kick, kick]);
  console.log('Strata startup respects User Settings and occupied ports, checks installed model identity, and delegates one service instance to launchd.');
}

verify().catch(error => {console.error(error); process.exitCode = 1;});
