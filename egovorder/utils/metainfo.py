import re

URL = 'url'
DATE = 'date'

_illegal_xml_chars_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')

def replace_xml_illegal_chars(val, replacement=' '):
    """Filter out characters that are illegal in XML."""

    return _illegal_xml_chars_RE.sub(replacement, val)

class MetaInfo(dict):
    def __init__(self):
        dict.__init__(self)

    def copy(self):
        m = MetaInfo()
        for k, v in self.items():
            m[k] = v
        return m

    def set_field(self, field, value):
        if type(value) in (str,):
            value = replace_xml_illegal_chars(value)
        self.__setitem__(field, value)

    def get_field(self, field):
        if field in self:
            return self.get(field)
        return None

    def set_date(self, value):
        self.set_field(DATE, value)

    def set_url(self, value):
        self.set_field(URL, value)

    def get_url(self):
        return self.get_field(URL)

    def get_date(self):
        return self.get_field(DATE)

    def add_desc_recursive(self, k, v, desc):
        if type(v) in (str,):
            v = v.strip()
            if v:
                desc.append((k.title(), v))
            return

        if type(v) in (list,):
            for i,vitem in enumerate(v):
                self.add_desc_recursive(f'{k.title()}.{i+1}', vitem, desc)
            return

        if type(v) in (dict,):
            for kv,vv in v.items():
                self.add_desc_recursive(f'{k.title()}.{kv.title()}', vv, desc)
            return

        if v:
            desc.append((k.title(), v))

    def get_ia_goir_description(self, srcinfo):
        desc = []

        keys = [ \
          ('gotype',      'Order Type'),  \
          ('gonum',       'Order Number'), \
          ('date',        'Date'), \
          ('department',  'Department'), \
          ('category',    'Category'), \
          ('section',     'Section'), \
          ('url',         'Order Source'), \
          ('abstract',    'Abstract'), \
        ]

        member_keys = set(self.keys())
        for k, kdesc in keys:
             if k not in member_keys:
                 continue

             v = self.get(k)
             if k == 'date':
                 v = f'{v}'
             elif k == 'url':
                v = f'<a href="{v}">URL</a>'
             else:
                 v = self.get(k).strip()

             if v:
                 desc.append((kdesc, v))

        known_keys = set([k for k, kdesc in keys])

        for k, v in self.items():
            if k not in known_keys:
                self.add_desc_recursive(k, v, desc)

        # style copied from orgpedia's MaharashtraGRs
        header = f'<b>{srcinfo["category"]}</b><br><br>'

        def get_row(d):
            key_cell = f'<td style="vertical-align: top"><b>{d[0]}: </b></td>'
            val_cell = f'<td style="vertical-align: bottom">{d[1]}</td>'
            return f'<tr>{key_cell} {val_cell}</tr>'

        rows_html = '\n'.join([get_row(d) for d in desc])

        return f'{header}<p><table>\n{rows_html}\n</table></p>'

    def get_ia_goir_title(self, srcinfo):
        category = srcinfo['category']
        title = [category]

        date = self.get('date', None)
        if date is not None:
            title.append(f'{date}')

        department = self.get('department', None)
        if department is not None:
            title.append(department.title())

        gotype = self.get('gotype', None)
        if gotype is not None:
            title.append(gotype.title())

        gonum = self.get('gonum', None)
        if gonum is not None:
            title.append(f'Number {gonum}')

        return ', '.join(title)

    def get_ia_metadata(self, srcinfo, to_sandbox):
        creator   = srcinfo.get('source')
        category  = srcinfo['category']

        # Allow metainfo to override language from srcinfo
        # If metainfo has 'language' field, use it; otherwise use srcinfo languages
        if 'language' in self:
            languages = self.get('language')
            # Convert to list if it's a string
            if isinstance(languages, str):
                languages = [languages]
        else:
            languages = srcinfo['languages']

        if to_sandbox:
            collection = 'test_collection'
        else:
            collection = srcinfo['collection']

        title = self.get_ia_goir_title(srcinfo)

        # Use creator from metainfo if available, otherwise use source
        metainfo_creator = self.get('creator', None)
        if metainfo_creator is not None and metainfo_creator.strip():
            creator = metainfo_creator.strip()

        metadata = { \
            'mediatype' : 'texts', 'language' : languages, \
            'title'     : title, \
            'subject'   : category
        }

        if creator:
            metadata['creator'] = creator

        if collection != '':
            metadata['collection'] = collection

        dateobj = self.get_date()
        if dateobj:
            metadata['date'] = f'{dateobj}'
        else:
            year = self.get('year', None)
            if year is not None:
                metadata['date'] = f'{year}'

        publisher = self.get('publisher', None)
        if publisher is not None and publisher.strip():
            metadata['publisher'] = publisher.strip()

        metadata['description'] = self.get_ia_goir_description(srcinfo)

        return metadata
