const crypto = require('node:crypto');

function policy(vscode, workspace, registryHash) {
  if (!vscode.workspace.isTrusted) throw new Error('Tools require a trusted workspace.');
  const config = vscode.workspace.getConfiguration('karma');
  const mode = config.get('tools.mode', 'ask');
  const input = config.get('tools.preapproved', {});
  if (!['disabled', 'ask', 'preapproved'].includes(mode) || !input || Array.isArray(input) || typeof input !== 'object') throw new Error('Invalid tool policy.');
  const allowed = {};
  for (const name of Object.keys(input).sort()) {
    if (!['ruff', 'pyright'].includes(name) || !Array.isArray(input[name]) || input[name].some(a => !['install', 'check'].includes(a))) throw new Error('Only registered tool operations can be preapproved.');
    allowed[name] = [...new Set(input[name])].sort();
  }
  const value = {workspace, registryHash, mode, allowed};
  return {...value, hash: crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex')};
}

function controller(vscode, context, deps) {
  const key = 'sutra.approvedToolPolicy.v1';
  const approvals = new WeakMap();
  const cancelled = token => { if (token?.isCancellationRequested) throw new Error('Tool approval cancelled.'); };
  async function approve(p, token) {
    cancelled(token);
    const before = policy(vscode, deps.workspace(), p.binding.registry_hash);
    if (before.mode === 'disabled') throw new Error('Optional tools are disabled in Sutra settings.');
    if (p.binding.workspace !== before.workspace) throw new Error('Tool proposal belongs to another workspace.');
    if (!deps.armed()) throw new Error('Grant Sutra workspace access before using tools.');
    let kind = 'user';
    if (before.mode === 'preapproved' && before.allowed[p.capability]?.includes(p.action)) {
      if (context.workspaceState.get(key) !== before.hash) {
        const choice = await vscode.window.showWarningMessage('Approve this workspace tool policy?', {modal: true,
          detail: 'These operations may run without another prompt in this workspace:\n' + JSON.stringify(before.allowed, null, 2) +
            '\nInstall downloads pinned packages into Sutra storage. Check reads only task-scoped Python snapshots, with no project writes or tool network. Registry or policy changes require approval again.'}, 'Approve policy');
        cancelled(token);
        if (choice !== 'Approve policy') return null;
        if (policy(vscode, deps.workspace(), p.binding.registry_hash).hash !== before.hash || !deps.armed()) throw new Error('Tool policy or workspace permissions changed during approval.');
        await context.workspaceState.update(key, before.hash);
      }
      kind = 'workspace_policy';
    } else {
      const detail = p.action === 'install'
        ? `Download ${p.capability} ${p.version} from ${p.download.url}\nIntegrity: ${p.download.integrity}\nMaximum download: ${p.download.max_download_bytes} bytes. Extract into Sutra storage; no package scripts or project dependency changes. This approval authorizes this registry download only.${p.repair_existing ? '\nThe previous tool copy will be quarantined and replaced by this verified download.' : ''}`
        : `Run ${p.capability} ${p.version} on these source snapshots:\n${Object.keys(p.source_versions).join('\n')}\nNo network, project writes, fixes or repository configuration. Tool diagnostics do not prove correctness.`;
      const choice = await vscode.window.showWarningMessage(`Sutra: ${p.action === 'install' ? 'install' : 'run'} ${p.capability}?`,
        {modal: true, detail: detail + `\nTask charge: ${p.cost.operations} operation, up to ${p.cost.seconds}s and ${p.cost.source_bytes} source bytes.`}, 'Approve');
      cancelled(token);
      if (choice !== 'Approve') return null;
    }
    if (policy(vscode, deps.workspace(), p.binding.registry_hash).hash !== before.hash || !deps.armed()) throw new Error('Tool policy or workspace permissions changed during approval.');
    const approval=Object.freeze({kind, proposal_id:p.id, policy_hash:before.hash});
    approvals.set(approval,{proposal:p,issued:Date.now()});
    return approval;
  }
  function evidence(approval) {
    const record=approvals.get(approval);
    if(!record||!deps.armed())throw new Error('Approve the specific Sutra tool proposal before execution.');
    const {proposal:p,issued}=record;
    const {hash,...current}=policy(vscode,deps.workspace(),p.binding.registry_hash);
    if(current.mode==='disabled'||current.workspace!==p.binding.workspace||hash!==approval.policy_hash||
        (approval.kind==='workspace_policy'&&(current.mode!=='preapproved'||context.workspaceState.get(key)!==hash||
          !current.allowed[p.capability]?.includes(p.action))))
      throw new Error('Tool policy or workspace permissions changed before execution.');
    return {approval,policy:current,capability:p.capability,action:p.action,issued};
  }
  async function manage(token) {
    const data = await deps.request({op: 'capability_list'},token);
    const item = await vscode.window.showQuickPick(data.tools.map(t => ({label: t.id, description: `${t.version} · ${t.installed ? 'installed' : 'not installed'}`, tool: t})), {title: 'Sutra tools'});
    if (!item) return;
    if (!deps.armed()) throw new Error('Grant Sutra workspace access and start a task before installing or running tools.');
    const action = item.tool.installed ? 'check' : 'install';
    const state = await deps.request({op: 'state'},token);
    if (!state.session) throw new Error('Start a Sutra workspace task first.');
    const binding = {armed: true, session: state.session, contract_hash: state.contract.contract_hash, calibration_hash: state.calibration.binding.hash};
    const p = await deps.request({op: 'capability_preview', ...binding, capability: item.tool.id, action, paths: []},token);
    const authorization = await approve(p,token);
    if (!authorization) return;
    const result = await deps.request({op: 'capability_execute', ...binding, proposal_id: p.id, authorization},token);
    await deps.showResult(result);
  }
  async function configure() {
    deps.workspace();
    const mode = await vscode.window.showQuickPick([
      {label:'Ask each time',value:'ask'}, {label:'Preapprove selected operations',value:'preapproved'}, {label:'Disable optional tools',value:'disabled'}
    ], {title:'Sutra tool policy for this workspace'});
    if (!mode) return;
    let allowed = {};
    if (mode.value === 'preapproved') {
      const items = ['ruff','pyright'].flatMap(name=>['install','check'].map(action=>({label:`${name}: ${action}`,name,action})));
      const selected = await vscode.window.showQuickPick(items,{title:'Choose operations to preapprove',canPickMany:true});
      if (!selected) return;
      for (const item of selected) (allowed[item.name] ||= []).push(item.action);
    }
    const config=vscode.workspace.getConfiguration('karma');
    await config.update('tools.preapproved',allowed,vscode.ConfigurationTarget.Workspace);
    await config.update('tools.mode',mode.value,vscode.ConfigurationTarget.Workspace);
    await context.workspaceState.update(key,undefined);
    await vscode.window.showInformationMessage('Tool policy saved. Sutra will ask before trusting preapprovals for this workspace and registry.');
  }
  return {approve, evidence, manage, configure};
}

function report(receipt) {
  const escape = x => String(x).replace(/[\\`*_{}\[\]<>#|]/g, '\\$&');
  const r = receipt.result;
  let text = `**${escape(receipt.capability)} · ${escape(r.status)}**\n\n`;
  if (r.status === 'installed') text += 'Installed the pinned tool in Sutra storage. Project dependencies are unchanged.\n';
  if (r.error) text += escape(r.error) + '\n';
  if (r.output) {
    let findings;
    try { const parsed = JSON.parse(r.output); findings = Array.isArray(parsed) ? parsed : parsed.generalDiagnostics; } catch {}
    if (Array.isArray(findings)) {
      text += `${findings.length} diagnostics in the approved snapshot.\n\n`;
      text += findings.slice(0,60).map(d => `- **${escape(d.filename || d.file || 'source')}:${d.location?.row || ((d.range?.start?.line ?? 0) + 1)}** — ${escape(d.message)}`).join('\n');
      if (findings.length > 60) text += '\n\nFull diagnostics are retained in the tool receipt.';
    } else text += escape(r.output.slice(0,6000));
    text += '\n\nStatic diagnostics are evidence, not proof of correctness.';
  }
  return text + '\n\nTool receipt: `' + receipt.receipt + '`';
}
module.exports = {controller, policy, report};
