# Adapted from egazette/utils/utils.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

from bs4 import BeautifulSoup, NavigableString, Tag

from . import proxylist

def get_proxydict(hostname):
    if hostname in proxylist.hostdict:
        return proxylist.hostdict[hostname]

    if '*' in proxylist.hostdict:
        return proxylist.hostdict['*']

    return None

def parse_webpage(webpage, parser):
    try:
        # Prefer the portals' UTF-8 over encoding guesses on sparse result pages.
        encoding = 'utf-8' if isinstance(webpage, bytes) else None
        d = BeautifulSoup(webpage, parser, from_encoding=encoding)
        return d
    except:
        return None

def get_tag_contents(node):
    if type(node) == NavigableString:
        return '%s' % node

    retval = []
    for content in node.contents:
        if type(content) == NavigableString:
            retval.append(content)
        elif type(content) == Tag and content.name not in ['style', 'script']:
            if content.name not in ['span']:
                retval.append(' ')
            retval.append(get_tag_contents(content))

    return ''.join(retval)
