"""Tool-free calibration workload. Only host OS observations become memory data."""
import re
from context_manager import InvalidUpdate

SYSTEM = 'This is a synthetic memory calibration workload. Read the fixture and return only {"ok":true}. No tools or actions are available. Do not reproduce the fixture.'
REQUEST = 'Measure a synthetic text-only memory workload.'
SCHEMA = {'type':'object','properties':{'ok':{'type':'boolean','enum':[True]}},
          'required':['ok'],'additionalProperties':False}


def check(s, sid, payload):
    from conversation import contract
    if set(payload) != {'contract','request','fixture','model'} or payload['contract'] != contract(s,sid):
        raise InvalidUpdate('Memory probe requires an unchanged tool-free contract')
    if payload['request'] != REQUEST or not isinstance(payload['model'],str):
        raise InvalidUpdate('Invalid memory probe request')
    if not isinstance(payload['fixture'],str) or not re.fullmatch(r'[0-9 ]{1,60000}',payload['fixture']):
        raise InvalidUpdate('Memory probe accepts only bounded synthetic digits')


def validate(s, sid, payload, proposal):
    check(s,sid,payload)
    return {'admission':{'admitted':True,'errors':[]},
            'checks':['schema','tool_free_contract','synthetic_fixture'],
            'provisional':True,'memory_measurement_authority':'host OS only'}
