"""Persistent source-span and dependency-aware context; proposals only."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from invocation_gateway import InvocationLedger as EvidenceContext
from runtime import ingest
from dag_runtime import run_paged
from local_model import LocalModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='evidence.sqlite')
    parser.add_argument('--logs', default='calls')
    parser.add_argument('--port', type=int, default=11434)
    parser.add_argument('--model', default='local-assistant:latest')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('new', 'inspect', 'toc', 'contract', 'calls'):
        p = commands.add_parser(name); p.add_argument('session')
        if name == 'toc':
            p.add_argument('--group'); p.add_argument('--offset', type=int, default=0)
    for name in ('ingest', 'run'):
        p = commands.add_parser(name); p.add_argument('session'); p.add_argument('message')
        if name == 'run':
            p.add_argument('--fact', action='append', default=[])
            p.add_argument('--policy-kind', choices=['retry', 'release'])
            p.add_argument('--policy-entry', default='policy-entry')
            p.add_argument('--policy-inputs', help='Host-supplied scenario inputs as a JSON object')
            p.add_argument('--max-repairs', type=int, default=2)
            p.add_argument('--max-calls', type=int, default=12)
            p.add_argument('--checkpoint', action='append', default=[], help='Accepted checkpoint to page in as untrusted derived context')
    for name in ('receipt', 'checkpoint', 'quarantine', 'call-receipt', 'quarantine-call'):
        p = commands.add_parser(name); p.add_argument('session'); p.add_argument('id')
        if name in ('quarantine', 'quarantine-call'): p.add_argument('reason')
    p = commands.add_parser('add-document')
    p.add_argument('session'); p.add_argument('id'); p.add_argument('file')
    p.add_argument('--expected-version', type=int, default=0)
    p = commands.add_parser('add-unit')
    p.add_argument('session'); p.add_argument('id'); p.add_argument('document')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--quote'); source.add_argument('--start', type=int)
    p.add_argument('--end', type=int)
    p.add_argument('--document-version', type=int)
    p.add_argument('--depends-on', action='append', default=[], help='Exact page reference ID@VERSION')
    p.add_argument('--fact', action='append', default=[], help='Host-attested KEY=VALUE within the source span')
    p.add_argument('--subject'); p.add_argument('--group', default='policy')
    p.add_argument('--expected-version', type=int, default=0)
    p = commands.add_parser('add-file')
    p.add_argument('session'); p.add_argument('logical_path'); p.add_argument('file')
    p.add_argument('--fact', action='append', default=[]); p.add_argument('--expected-version', type=int, default=0)
    p = commands.add_parser('add-claim')
    p.add_argument('session'); p.add_argument('id'); p.add_argument('text')
    p.add_argument('--key', action='append', required=True)
    args = parser.parse_args()
    store = EvidenceContext(args.db)
    try:
        if args.command == 'new':
            store.create(args.session); result = {'created': args.session}
        elif args.command == 'inspect':
            result = {'contract': store.snapshot(args.session), 'pages': store.report(args.session),
                      'audit_valid': store.audit_valid()}
        elif args.command == 'toc':
            result = store.catalog(args.session, args.group, args.offset)
        elif args.command == 'contract':
            result = store.current_contract(args.session)
        elif args.command == 'calls':
            result = []
            for iid, raw in store.db.execute('SELECT id,record_json FROM invocations WHERE session=?', (args.session,)):
                env = json.loads(raw)
                row = store.db.execute('SELECT id,record_json FROM invocation_receipts WHERE invocation_id=?', (iid,)).fetchone()
                result.append({'id': iid, 'call_id': env['call_id'], 'role': env['role'],
                               'receipt_id': row[0] if row else None,
                               'status_at_validation': json.loads(row[1])['status'] if row else 'pending'})
        elif args.command == 'call-receipt':
            row = store.db.execute('SELECT record_json FROM invocation_receipts WHERE id=? AND session=?', (args.id, args.session)).fetchone()
            if not row: raise ValueError('Call receipt not found')
            result = json.loads(row[0])  # Forensic inspection; proposal may be quarantined.
        elif args.command == 'quarantine-call':
            store.quarantine_invocation(args.session, args.id, args.reason)
            result = {'invocation_id': args.id, 'status': 'quarantined'}
        elif args.command == 'checkpoint':
            result = store.load_checkpoint(args.session, args.id)
        elif args.command == 'quarantine':
            store.quarantine(args.session, args.id, args.reason)
            result = {'checkpoint_id': args.id, 'status': store.checkpoint_status(args.session, args.id)}
        elif args.command == 'receipt':
            row = store.db.execute('SELECT record_json FROM assertion_receipts WHERE id=? AND session=?', (args.id, args.session)).fetchone()
            if not row: raise ValueError('Receipt not found in this session')
            result = json.loads(row[0])
        elif args.command == 'add-document':
            path = Path(args.file).resolve(); raw = path.read_bytes()
            doc = store.put_document(args.session, args.id, raw.decode('utf-8'), authority='file',
                source={'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}, expected_version=args.expected_version)
            result = {k: doc[k] for k in ('key', 'version', 'digest')}
        elif args.command == 'add-unit':
            doc = store.document(args.session, args.document, args.document_version)
            if args.quote is not None:
                if not args.quote or doc['text'].count(args.quote) != 1:
                    raise ValueError('Quote must occur exactly once; use explicit offsets to disambiguate')
                start = doc['text'].index(args.quote); end = start + len(args.quote)
            else:
                if args.end is None: raise ValueError('--end is required with --start')
                start, end = args.start, args.end
            dependencies = [(key, int(version)) for key, version in (x.rsplit('@', 1) for x in args.depends_on)]
            facts = dict(x.split('=', 1) for x in args.fact)
            page = store.put_unit(args.session, args.id, args.document, start, end, document_version=doc['version'],
                dependencies=dependencies, facts=facts, subject=args.subject, group=args.group, expected_version=args.expected_version)
            result = {'id': page.key, 'version': page.version, 'source': json.loads(page.source_json)}
        elif args.command == 'add-file':
            page = store.import_file(args.session, args.logical_path, args.file,
                facts=dict(x.split('=', 1) for x in args.fact), expected_version=args.expected_version)
            store.observe_file(args.session, 'file-' + uuid.uuid4().hex, args.logical_path, args.file, store.snapshot(args.session)['revision'])
            result = {'id': page.key, 'version': page.version}
        elif args.command == 'add-claim':
            page = store.put_claim(args.session, args.id, args.text, args.key)
            result = {'id': page.key, 'status': store.status(page)}
        else:
            model = LocalModel(args.logs, args.port, args.model)
            if args.command == 'ingest':
                result = ingest(store, args.session, args.message, model)
                if result['committed']: result['contract'] = store.current_contract(args.session)
            else:
                store.close(); store = None
                if bool(args.policy_kind) != bool(args.policy_inputs):
                    raise ValueError('--policy-kind and --policy-inputs are required together')
                policy_request = {'kind': args.policy_kind, 'entry_page': args.policy_entry,
                                  'inputs': json.loads(args.policy_inputs)} if args.policy_kind else None
                result = run_paged(args.db, args.session, args.message, args.fact, model, 'run-' + uuid.uuid4().hex,
                                   policy_request=policy_request, max_repairs=args.max_repairs,
                                   max_calls=args.max_calls, checkpoint_ids=args.checkpoint)
            result['call_ids'] = [r['call_id'] for r in result.pop('calls')]
        print(json.dumps(result, indent=2))
    finally:
        if store: store.close()


if __name__ == '__main__': main()
