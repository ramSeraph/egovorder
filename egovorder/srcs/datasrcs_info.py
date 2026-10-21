# Adapted from egazette/srcs/datasrcs_info.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

import re
from datetime import datetime

# Keep the existing sandbox prefix so archived test items and done lists match.
TEST_PREFIX = 'egaztest.'

srcinfos = {
    'andhra_goir': {
        'languages' : ['eng', 'tel'],
        'source'    : 'Government of Andhra Pradesh',
        'category'  : 'Andhra Pradesh Government Order',
        'start_date': datetime(2008, 6, 1),
        'prefix'    : 'in.gov.andhra.goir.',
        'collection': 'andhra-goir',
        'type'      : 'goir',
        'expected_update_frequency': 1,
        'expected_delay': 2
    },
    'telangana_goir': {
        'languages' : ['eng', 'tel'],
        'source'    : 'Government of Telangana',
        'category'  : 'Telangana Government Order',
        'start_date': datetime(2014, 6, 2),
        'prefix'    : 'in.gov.telangana.goir.',
        'collection': 'telangana-goir',
        'type'      : 'goir',
        'expected_update_frequency': 5,
        'expected_delay': 4
    },
    'uttarakhand_gos': {
        'languages' : ['eng', 'hin'],
        'source'    : 'Government of Uttarakhand',
        'category'  : 'Uttarakhand Government Order',
        'start_date': datetime(1999, 5, 13),
        'prefix'    : 'in.gov.uttarakhand.go.',
        'collection': 'uttarakhand-gos',
        'type'      : 'goir',
        'expected_update_frequency': 5,
        'expected_delay': 20
    },
}

def basic_identifier(relurl, metainfo, prefix_len):
    identifier = relurl.replace('/', '.')
    return identifier

def get_prefix(srcname, to_sandbox=False):
    srcinfo = srcinfos[srcname]
    prefix = srcinfo['prefix']
    if to_sandbox:
        prefix = TEST_PREFIX + prefix
    return prefix

def get_start_date(srcname):
    srcinfo = srcinfos[srcname]
    start_date = srcinfo.get('start_date', None)
    return start_date

def get_identifier(relurl, metainfo, to_sandbox):
    words   = relurl.split('/')
    srcname = words[0]
    relurl  = '/'.join(words[1:])

    relurl, _ = re.subn(r"[',&:%\s;()–]", '', relurl)

    srcinfo = srcinfos[srcname]

    identifier_fn = srcinfo.get('identifier_fn', basic_identifier)

    prefix = get_prefix(srcname, to_sandbox=to_sandbox)

    identifier = identifier_fn(relurl, metainfo, len(prefix))

    return prefix + identifier

def get_expected_update_frequency(srcname):
    srcinfo = srcinfos[srcname]
    return srcinfo.get('expected_update_frequency', 30)

def get_expected_delay(srcname):
    srcinfo = srcinfos[srcname]
    return srcinfo.get('expected_delay', 30)
