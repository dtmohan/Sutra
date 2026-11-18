"""Repeatable DOCX comparison fixture with approved and superseded records."""

import json
import re
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


NAME = 'northridge-rollout-dossier.docx'
REQUEST = (
    f'Review the named document {NAME} and prepare the release decision from the signed records. '
    'Distinguish current records from superseded draft entries. Compute the unused finance '
    'headroom after both commitments and protected contingency. Apply the release rule across '
    'all four sections. In your final answer return one JSON object with these keys: '
    'first_wave_sites, qa_floor_pct, qa_result_pct, unallocated_usd, restore_limit_minutes, '
    'restore_result_minutes, decision (GO or HOLD), blocking_record (signed record ID), and '
    'records (array of the four signed record IDs). Use numeric values for the six quantities.'
)

EXPECTED = {
    'first_wave_sites': 180,
    'qa_floor_pct': 98.5,
    'qa_result_pct': 98.9,
    'unallocated_usd': 37000,
    'restore_limit_minutes': 15,
    'restore_result_minutes': 16,
    'decision': 'HOLD',
    'blocking_record': 'REL-09',
}
APPROVED_RECORDS = ('OPS-42', 'QA-18', 'FIN-37', 'REL-09')
DRAFT_VALUES = {
    'first_wave_sites': 210,
    'qa_floor_pct': 96,
    'qa_result_pct': 97.4,
    'unallocated_usd': 92000,
    'restore_limit_minutes': 20,
    'restore_result_minutes': 12,
    'decision': 'GO',
}

# The background paragraphs are realistic case notes. They deliberately carry
# no other approved values that could be confused with the signed entries.
BACKGROUND = (
    'The desk assigned {topic} to {owner} for continuing observation. In the meeting, {concern}. '
    'The journal describes how this work is carried out during handover and how the receiving desk '
    'can reproduce the check. The participants retained the earlier wording for traceability and '
    'agreed to carry open questions forward in the exception register. The current determination '
    'for this section resides in the signed record, while this paragraph explains the process.',
    'For {topic}, {owner} walked through the handoff with the relief team. They recorded that '
    '{concern}. Both teams compared the paper checklist with the operational log and highlighted '
    'where a remote station could follow a local practice without affecting the central record. '
    'The reviewer can reconstruct the procedure from the packet and can raise an exception when '
    'material observed in the field disagrees with the written instructions.',
    '{owner} attached field notes about {topic}. The notes explain that {concern}. The coordinator '
    'asked the relief shift to practice the sequence using a sample request and retain the result '
    'alongside the training log. The attachment helps resolve process questions if the receiving '
    'crew discovers an obsolete checklist during deployment. Where the attachment and signed '
    'decision differ, the signed entry supplies the release value.',
    'During a tabletop walk-through, {owner} presented the procedure for {topic}. The procedure '
    'preserves the original check result and records any later correction separately. The group '
    'reported that {concern}. The owner cross-checked the naming convention with the records desk '
    'so a reviewer can follow the revision history to its current entry. The permanent packet '
    'keeps the annotated walk-through available for engineering and operations.',
)

SECTIONS = (
    {
        'heading': 'Section 1 - Operations and first-wave scope',
        'intro': (
            'Northridge is moving a distributed monitoring service onto a new release. This dossier '
            'collects field readiness, validation, finance, and service restoration records. The '
            'working drawings remain attached because supervisors already trained from them. '
            'Each signed record below supplies the current decision inputs for its own section. '
            'Draft annotations remain available to explain how the process changed.'
        ),
        'topics': (
            ('site roster maintenance', 'the field scheduling desk', 'the warehouse may receive late substitutions'),
            ('duplicate station labels', 'the records lead', 'short names may refer to more than one station'),
            ('spare bench placement', 'the inventory coordinator', 'remote crews need a clean spare during the cutover'),
            ('local maintenance windows', 'the dispatch desk', 'overlapping shifts need a common escalation channel'),
            ('operator sign-on', 'the training lead', 'visiting operators require the regular handover brief'),
            ('central dashboard labels', 'the instrumentation team', 'display labels can lag behind the physical roster'),
            ('handover traffic', 'the floor supervisor', 'an active maintenance lane needs a protected route'),
            ('building access records', 'the facilities desk', 'a key transfer may precede entry in the access log'),
            ('roster corrections', 'the distribution analyst', 'corrections need an independent receiver sign-off'),
            ('field package revision', 'the service transition lead', 'old checklists can remain in shared cabinets'),
            ('retired station inventory', 'the asset custodian', 'stored stations sometimes appear active on old lists'),
            ('dispatch control references', 'the district controller', 'rural shifts may have limited radio coverage'),
            ('operator coverage handoff', 'the duty allocation desk', 'a second receiver reviews substitutions'),
            ('exception follow-ups', 'the rollout secretariat', 'the exception register should name each open action'),
        ),
        'approved': (
            'SIGNED CURRENT RECORD OPS-42. The approved first-wave scope is 180 sites. This is the '
            'size of the first wave if release is allowed by the signed cross-team rule in REL-09. '
            'OPS-42 replaces the enrollment forecasts in the scheduling draft.'
        ),
        'draft': (
            'SUPERSEDED WORKING COPY OPS-D7. The old enrollment exercise estimated 210 sites for the '
            'first wave and used a provisional flexible scope. The estimate remains in the attachment '
            'as an explanation of the old staffing forecast. OPS-42 replaced it.'
        ),
    },
    {
        'heading': 'Section 2 - Validation and qualification',
        'intro': (
            'The validation program covers record-level checks of the new monitoring service. '
            'The current signed qualification measures the share of cases accepted at the '
            'release gate. Drill performance belongs to the separate restoration record. '
            'Training sessions can include deliberately flawed examples and remain outside '
            'the signed qualification denominator.'
        ),
        'topics': (
            ('fixture labeling', 'the qualification desk', 'temporary labels can survive a handover'),
            ('coverage registers', 'the validation lead', 'the spare line is exercised during orientation'),
            ('control case review', 'the instrument technician', 'reference fixtures need an independent sign-off'),
            ('batch identification', 'the data custodian', 'a retest needs a new log line under the same batch'),
            ('reviewer succession', 'the audit coordinator', 'the night shift retains its own access checklist'),
            ('evidence retention', 'the analysis team', 'raw files and derived spreadsheets use different archives'),
            ('data corrections', 'the results secretary', 'a corrected row needs a reason and a preserved original'),
            ('export review', 'the independent reviewer', 'a forwarded worksheet can contain an obsolete tab'),
            ('exception disposition', 'the defect coordinator', 'open findings need an owner before the final review'),
            ('control failures', 'the qualification engineer', 'a known training result should stay out of the signed results'),
            ('shift coverage', 'the laboratory duty officer', 'a relief operator may need a read-only register view'),
            ('qualification archive', 'the records coordinator', 'working files retain their draft markings'),
            ('review queue', 'the instrument supervisor', 'station identifiers need to survive the export process'),
            ('qualification handoff', 'the test program lead', 'the receiving crew checks the revision stamp'),
        ),
        'approved': (
            'SIGNED CURRENT RECORD QA-18. The minimum qualification rate is 98.5 percent. '
            'The signed run accepted 989 of 1,000 cases, a qualification rate of 98.9 percent. '
            'QA-18 is the current qualification gate and the completed signed result for release.'
        ),
        'draft': (
            'SUPERSEDED WORKING COPY QA-D4. The rehearsal worksheet used a qualification floor '
            'of 96 percent and forecast a 97.4 percent result. These values describe training '
            'materials retained for audit and have been replaced by QA-18.'
        ),
    },
    {
        'heading': 'Section 3 - Finance controls and protected contingency',
        'intro': (
            'Finance maintains a funding ceiling for the first wave. The project may spend only '
            'the funds remaining after the signed commitment ledger and protected contingency '
            'are charged. The supplier forecasting worksheet explains why working totals differ '
            'from the approved amounts. It stays in the dossier to support invoice reconciliation.'
        ),
        'topics': (
            ('invoice segregation', 'the controller', 'a deposit and its invoice can appear on separate worksheets'),
            ('contingency coding', 'the budget analyst', 'emergency buying uses a separate authorization path'),
            ('supplier questions', 'the purchasing desk', 'a quote can appear before a commitment exists'),
            ('cost center alignment', 'the ledger custodian', 'a corrected code requires a preserved original entry'),
            ('expense submissions', 'the field accountant', 'manual receipts need the ordinary review schedule'),
            ('renewal treatment', 'the commercial lead', 'a renewal option stays visible in the contract index'),
            ('forecast reconciliation', 'the planning desk', 'rehearsal spreadsheets sometimes circulate in email'),
            ('interoffice transfers', 'the internal controls officer', 'transfers need a matching entry at the destination'),
            ('supplier confirmation', 'the accounts lead', 'purchase orders can be split across systems'),
            ('purchase handoff', 'the finance coordinator', 'the relief officer needs both the ledger and the revision stamp'),
            ('invoice dispute review', 'the audit clerk', 'undisputed lines can proceed during review of a corrected charge'),
            ('released funds', 'the funding committee secretary', 'a provisional forecast can outlive its approval meeting'),
            ('reserved purchases', 'the discretionary spend lead', 'uncommitted funds remain visible for planning'),
            ('control memo circulation', 'the finance secretariat', 'a released memo supersedes the meeting worksheet'),
        ),
        'approved': (
            'SIGNED CURRENT RECORD FIN-37. The approved spending ceiling is USD 860,000. '
            'Signed commitments total USD 724,000. The protected contingency is USD 99,000. '
            'Compute unallocated headroom as the ceiling less signed commitments less protected '
            'contingency. Only nonnegative unallocated headroom can pass the finance condition.'
        ),
        'draft': (
            'SUPERSEDED WORKING COPY FIN-D2. The budget workshop modeled a USD 915,000 ceiling and '
            'showed USD 92,000 unallocated in the forecast. This was a scenario for negotiation '
            'and must not be used for release, because FIN-37 contains the signed ceiling.'
        ),
    },
    {
        'heading': 'Section 4 - Service restoration and release disposition',
        'intro': (
            'The restoration exercise replays a failure after deployment and tracks the interval '
            'until service is usable by field operators. Scenario timing logs may cover training '
            'on individual actions. Use the signed end-to-end exercise for the release test. '
            'The signed rule also points to the current records in the other sections.'
        ),
        'topics': (
            ('exercise log capture', 'the incident coordinator', 'console clocks need a shared reference'),
            ('operator communications', 'the dispatch lead', 'shift substitutions can change the usual call order'),
            ('safety isolation procedure', 'the service control desk', 'the team needs to retain a separate test channel'),
            ('alternate restoration route', 'the technical lead', 'a staged exercise may pause while staff review a cue'),
            ('normalization checks', 'the operations supervisor', 'a system can restore telemetry before operators resume work'),
            ('duty role transfers', 'the incident manager', 'an acting lead needs to accept the handoff explicitly'),
            ('exercise form revisions', 'the records owner', 'draft measures may appear in the printed training guide'),
            ('incident artifact storage', 'the evidence custodian', 'reviewers may need independent access to timing logs'),
            ('review observation handling', 'the section secretary', 'the reviewer keeps handwritten notes alongside the report'),
            ('contingency route training', 'the relief crew', 'training cases may omit parts of the live workflow'),
            ('retest preparation', 'the restoration lead', 'the facilitator rehearses incident announcements before a run'),
            ('communications dry run', 'the local radio desk', 'an operator may use the dispatch channel during the test'),
            ('desk direction notice', 'the shift coordinator', 'an outdated notice may still circulate during handovers'),
            ('release packet routing', 'the release secretary', 'the approver receives signed statements in order'),
        ),
        'approved': (
            'SIGNED CURRENT RECORD REL-09. Release is GO only if QA-18 meets its qualification '
            'floor, FIN-37 leaves nonnegative unallocated headroom, and the signed restoration '
            'exercise finishes within its 15-minute limit. Otherwise the release disposition '
            'is HOLD. The signed end-to-end exercise finished in 16 minutes. Ari Soto owns '
            'the restoration retest. OPS-42 authorizes first-wave scope after the gates pass.'
        ),
        'draft': (
            'SUPERSEDED WORKING COPY REL-D2. Training assumed a 20-minute limit and simulated '
            'restoration in 12 minutes, with a provisional disposition of GO. REL-09 replaced '
            'both numbers and the provisional conclusion for the live release decision.'
        ),
    },
)


def paragraphs():
    """Interleave source facts and draft traps with relevant review context."""
    for section in SECTIONS:
        yield True, section['heading']
        yield False, section['intro']
        for index, (topic, owner, concern) in enumerate(section['topics']):
            if index == 5:
                yield False, section['approved']
            if index == 10:
                yield False, section['draft']
            template = BACKGROUND[index % len(BACKGROUND)]
            yield False, template.format(topic=topic, owner=owner, concern=concern)


def write_document(path):
    """Write deterministic DOCX bytes and return the expected one-line-per-paragraph text."""
    content = list(paragraphs())
    body = []
    for heading, paragraph in content:
        style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if heading else ''
        body.append(f'<w:p>{style}<w:r><w:t>{escape(paragraph)}</w:t></w:r></w:p>')
    parts = {
        '[Content_Types].xml': (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ),
        '_rels/.rels': (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        'word/document.xml': (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{"".join(body)}</w:body></w:document>'
        ),
    }
    with ZipFile(Path(path), 'w') as document:
        for name, data in parts.items():
            entry = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            document.writestr(entry, data, compress_type=ZIP_DEFLATED)
    return '\n'.join(text for _, text in content)


def _structured_answer(answer):
    if isinstance(answer, dict):
        if any(key in answer for key in EXPECTED):
            return answer
        answer = answer.get('answer')
    if not isinstance(answer, str):
        return None
    candidates = re.findall(r'```(?:json)?\s*(.*?)\s*```', answer, re.DOTALL | re.IGNORECASE)
    for text in (answer, *candidates):
        try:
            parsed = json.loads(text.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _same(value, expected):
    if type(expected) in (int, float):
        return type(value) in (int, float) and value == expected
    return isinstance(value, str) and value.strip().upper() == expected


def grade(answer):
    """Score the same exact fields and signed-record coverage in both conditions."""
    parsed = _structured_answer(answer)
    actual = parsed or {}
    fields = {key: _same(actual.get(key), value) for key, value in EXPECTED.items()}
    records = actual.get('records')
    observed = {item.strip().upper() for item in records if isinstance(item, str)} if isinstance(records, list) else set()
    sources = {record: record in observed for record in APPROVED_RECORDS}
    stale = [key for key, value in DRAFT_VALUES.items() if _same(actual.get(key), value)]
    return {
        'json_object': parsed is not None, 'fields': fields, 'sources': sources,
        'correct_fields': sum(fields.values()), 'correct_sources': sum(sources.values()),
        'superseded_fields_used': stale, 'unexpected_sources': sorted(observed - set(APPROVED_RECORDS)),
        'exact': all(fields.values()) and all(sources.values()) and observed == set(APPROVED_RECORDS),
    }
