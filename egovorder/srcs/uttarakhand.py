# Adapted from egazette/srcs/uttarakhand.py on the workspace branch.
# Modified for government-order downloads; see LICENSE and COPYING.

from datetime import datetime
from http.cookiejar import CookieJar
import json
import os
import urllib.parse

from .baseorder import BaseOrder
from ..utils import ext_ops
from ..utils.metainfo import MetaInfo


class UttarakhandGO(BaseOrder):
    def __init__(self, name, storage):
        super().__init__(name, storage)
        self.baseurl = 'https://go.uk.gov.in/'
        self.base_endp = 'en/Search/index'
        self.search_endp = 'en/Search/SearchGO'
        self.hostname = 'go.uk.gov.in'

    def get_postdata(self, dateobj):
        date_str = dateobj.strftime('%Y-%m-%d')
        return json.dumps({
            'CategoryID': '', 'DepartmentID': '', 'GONo': '',
            'SearchText': '', 'SectionID': '', 'Subject': '',
            'fromdate': date_str, 'todate': date_str,
        }).encode('utf8')

    def get_metainfo(self, row, dateobj):
        # Reject a changed/ignored date filter before archiving under the wrong day.
        if row.get('GODate1678'):
            source_date = datetime.strptime(row['GODate1678'], '%d-%m-%Y').date()
            if source_date != dateobj:
                raise ValueError(f'Result date {source_date} differs from requested {dateobj}')
        metainfo = MetaInfo()
        metainfo.set_date(dateobj)
        for key, field in (
            ('subject', 'Subject'), ('department', 'DepartmentNameE'),
            ('section', 'SectionNameE'), ('category', 'CategoryNameE'),
            ('gonum', 'GONo'),
        ):
            metainfo.set_field(key, str(row[field]) if row[field] is not None else '')
        metainfo['gotype'] = 'Amendment' if row['G0Type'] == 'A' else 'New'
        metainfo['url'] = row['FilePath'] or row['File_Path_Word']
        if not isinstance(metainfo['url'], str) or not metainfo['url'].strip():
            raise ValueError('Missing PDF and Word document links')
        return metainfo

    def document_location(self, path):
        legacy_url = urllib.parse.urljoin(self.baseurl, path.replace('http://', 'https://'))
        # Retain the old filename-derived identifiers, including PDF\\ -> PDF-.
        docid = urllib.parse.urlsplit(legacy_url).path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        docid = docid.replace('\\', '-')
        if not docid or docid in ('.', '..'):
            raise ValueError('Missing document filename')
        url = urllib.parse.urljoin(self.baseurl, path.replace('\\', '/'))
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('Invalid document URL')
        return docid, parts._replace(scheme='https').geturl()

    def is_valid_order(self, doc, minsize):
        return super().is_valid_order(doc, minsize) and ext_ops.get_buffer_type(doc) != 'text/html'

    def download_oneday(self, relpath, dateobj):
        cookiejar = CookieJar()
        baseurl = urllib.parse.urljoin(self.baseurl, self.base_endp)
        response = self.download_url(baseurl, savecookies=cookiejar)
        if not response or not response.webpage:
            self.download_error('Could not get base page for date %s', dateobj)
            return []

        searchurl = urllib.parse.urljoin(self.baseurl, self.search_endp)
        response = self.download_url(
            searchurl, postdata=self.get_postdata(dateobj), encodepost=False,
            headers={'Content-Type': 'application/json'}, referer=baseurl,
            loadcookies=cookiejar, savecookies=cookiejar,
        )
        if not response or not response.webpage:
            self.download_error('Could not download search results for date %s', dateobj)
            return []
        try:
            rows = json.loads(response.webpage)
            if not isinstance(rows, list):
                raise ValueError('Expected a JSON list of orders')
        except (ValueError, UnicodeError) as exc:
            self.download_error('Unable to parse search results for %s: %s', dateobj, exc)
            return []

        downloads = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError('Expected an order object')
                metainfo = self.get_metainfo(row, dateobj)
                docid, url = self.document_location(metainfo.pop('url'))
            except (KeyError, TypeError, ValueError) as exc:
                self.download_error('Invalid order record for %s: %s', dateobj, exc)
                continue
            relurl = os.path.join(relpath, docid)
            if self.save_order(relurl, url, metainfo, referer=baseurl, cookiefile=cookiejar):
                downloads.append(relurl)
        return downloads
