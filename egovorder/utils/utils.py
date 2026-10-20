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
        d = BeautifulSoup(webpage, parser)
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
