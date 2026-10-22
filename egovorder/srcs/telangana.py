# Adapted from egazette/srcs/telangana.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

from http.cookiejar import CookieJar
import os
import re
import urllib.parse

from .baseorder import BaseOrder
from ..utils import utils
from ..utils import ext_ops
from ..utils.metainfo import MetaInfo


class TelanganaGOIR(BaseOrder):
    def __init__(self, name, storage):
        BaseOrder.__init__(self, name, storage)
        self.baseurl      = 'https://goir.telangana.gov.in/'
        self.hostname     = 'goir.telangana.gov.in'
        self.search_endp  = './'

    def remove_fields(self, postdata, fields):
        newdata = []
        for k, v in postdata:
            if k not in fields:
                newdata.append((k, v))
        return newdata


    def replace_field(self, formdata, k, v):
        newdata = []
        for k1, v1 in formdata:
            if k1 == k:
                newdata.append((k1, v))
            else:
                newdata.append((k1, v1))
        return newdata

    def get_search_form(self, webpage, endp):
        d = utils.parse_webpage(webpage, self.parser)
        if d is None:
            return None

        search_form = d.find('form', {'action': endp})
        return search_form

    def get_selected_option(self, select):
        option = select.find('option', {'selected': 'selected'})
        if option is None:
            option = select.find('option')
        if option is None:
            return ''
        val = option.get('value')
        if val is None:
            val = ''
        return val

    def get_form_data(self, webpage, dateobj):
        search_form = self.get_search_form(webpage, self.search_endp)
        if search_form is None:
            return None

        reobj  = re.compile(r'^(input|select)$')
        inputs = search_form.find_all(reobj)

        formdata = []
        for tag in inputs:
            name  = None
            value = None

            if tag.name == 'input':
                name  = tag.get('name')
                value = tag.get('value')
                t     = tag.get('type')
                if t in ['image', 'checkbox', 'button', 'submit']:
                    continue
            elif tag.name == 'select':
                name = tag.get('name')
                value = self.get_selected_option(tag)
            if name:
                if value is None:
                    value = ''
                formdata.append((name, value))

        datestr = dateobj.strftime('%d/%m/%Y')
        formdata = self.replace_field(formdata, 'txtfrmdate', datestr)
        formdata = self.replace_field(formdata, 'txttodate',  datestr)
        formdata = self.remove_fields(formdata, set(['sectddl']))

        return formdata

    def get_column_order(self, tr):
        order = []
        for td in tr.find_all('th'):
            txt = utils.get_tag_contents(td)
            if txt and re.search(r'Department', txt):
                order.append('department')
            elif txt and re.search(r'Section', txt):
                order.append('section')
            elif txt and re.search(r'Type', txt):
                order.append('gotype')
            elif txt and re.search(r'GO\s+No', txt):
                order.append('gonum')
            elif txt and re.search(r'Category', txt):
                order.append('category')
            elif txt and re.search(r'Abstract', txt):
                order.append('abstract')
            elif txt and re.search(r'Download', txt):
                order.append('download')
            else:
                order.append('')
        return order

    def find_next_page(self, d, curr_page):
        div = d.find('div', {'id': 'paging'})
        if div is None:
            return None
        links = div.find_all('a')
        for link in links:
            txt = utils.get_tag_contents(link)
            if txt:
                try:
                    page_no = int(txt)
                except Exception:
                    page_no = None
                if page_no == curr_page + 1:
                    return link

        return None

    def process_result_row(self, tr, metainfos, dateobj, order):
        metainfo = MetaInfo()
        metainfo.set_date(dateobj)

        i = 0
        for td in tr.find_all('td'):
            if len(order) > i:
                col = order[i]
                txt = utils.get_tag_contents(td)
                if txt:
                    txt = txt.strip()

                if col == 'gotype':
                    if txt == 'MS':
                        metainfo[col] = 'Manuscript'
                    elif txt == 'RT':
                        metainfo[col] = 'Routine'
                    else:
                        metainfo[col] = txt
                elif col == 'download':
                    a = td.find('a')
                    if a is not None:
                        metainfo[col] = a
                elif col != '':
                    metainfo[col] = txt
            i += 1

        if len(metainfo) == 8:
            metainfos.append(metainfo)


    def parse_search_results(self, webpage, dateobj, curr_page):
        metainfos = []
        nextpage  = None

        d = utils.parse_webpage(webpage, self.parser)
        if not d:
            self.download_error('Unable to parse search result page for %s', dateobj)
            return metainfos, nextpage

        tables = d.find_all('table', {'id': 'FileMoveList2'})

        if not tables and self.get_search_form(webpage, self.search_endp) is not None and re.search(
                r'\bno (?:records?|results?|data) (?:found|available)\b', d.get_text(' ', strip=True), re.I):
            return metainfos, nextpage
        if len(tables) != 1:
            self.download_error('Could not find the result table for %s', dateobj)
            return metainfos, nextpage

        order = None
        for tr in tables[0].find_all('tr'):
            if not order:
                order = self.get_column_order(tr)
                continue

            self.process_result_row(tr, metainfos, dateobj, order)

        nextpage = self.find_next_page(d, curr_page)

        return metainfos, nextpage

    def download_nextpage(self, nextpage, search_url, postdata, cookiejar):
        newdata = []
        href = nextpage.get('href')
        if not href:
            self.download_error('Missing next-page link')
            return None

        reobj = re.search(r'__doPostBack\(\'(?P<etarget>[^\']+)', href)
        if not reobj:
            self.download_error('Could not get link for next page')
            return None

        etarget = reobj.groupdict()['etarget']

        newdata = self.replace_field(postdata, '__EVENTTARGET', etarget)

        response = self.download_url(search_url, savecookies = cookiejar, \
                                     referer = search_url, \
                                     loadcookies = cookiejar, \
                                     postdata = newdata)

        return response

    def download_metainfos(self, relpath, metainfos, searchurl, \
                           postdata, cookiejar):
        dls = []
        for metainfo in metainfos:
            if 'download' in metainfo:
                download = metainfo.pop('download')
                href = download.get('href')
                if not href:
                    self.download_error('Missing order download link')
                    continue

                groups = []
                for reobj in re.finditer(r"'(?P<obj>[^']*)'", href):
                    groups.append(reobj.groupdict()['obj'])

                if not groups or len(groups) < 2:
                    self.download_error('Unrecognized order download link')
                    continue

                etarget   = groups[0]
                eargument = groups[1]

                newpost = postdata[:]
                newpost = self.replace_field(newpost, '__EVENTTARGET', etarget)
                newpost = self.replace_field(newpost, '__EVENTARGUMENT', eargument)

                metainfo.set_url(self.url_fix(self.baseurl))

                dept    = metainfo['department'].lower()
                dept, _ = re.subn(r"[',&:%\s;()–]", '-', dept)

                gonum  = metainfo['gonum']
                gotype = metainfo['gotype'].lower()
                docid  = f'{dept}-{gotype}-{gonum}'
                relurl = os.path.join(relpath, docid)
                # The portal reserves numbers for unissued orders and still
                # shows a download link, which returns the search page as HTML.
                if re.fullmatch(r'\s*not\s+issued\s*\.?\s*',
                                metainfo.get('abstract') or '', re.I):
                    self.logger.info('Skipping order marked Not Issued: %s', relurl)
                    continue
                if self.save_order(relurl, searchurl, metainfo, postdata=newpost, \
                                     referer = searchurl, cookiefile = cookiejar, validurl=False):
                    dls.append(relurl)
        return dls

    def is_valid_order(self, doc, minsize):
        if not BaseOrder.is_valid_order(self, doc, minsize):
            return False

        mtype = ext_ops.get_buffer_type(doc)

        return mtype != 'text/html'

    def missing_order_reason(self, doc):
        if ext_ops.get_buffer_type(doc) != 'text/html':
            return None
        page = utils.parse_webpage(doc, self.parser)
        if page is not None:
            # The portal returns the search page with this alert when an issued
            # order has no uploaded document. Other HTML responses remain errors.
            for script in page.find_all('script'):
                if re.search(
                        r"\bbootbox\.alert\s*\(\s*\{[^}]*\bmessage\s*:\s*"
                        r"(['\"])\s*Go\s+Text\s+Not\s+Uploaded\s*\1",
                        script.get_text(), re.I):
                    return 'Go Text Not Uploaded'
        return None



    def download_oneday(self, relpath, dateobj):
        dls = []
        cookiejar  = CookieJar()

        response = self.download_url(self.baseurl, savecookies = cookiejar)
        if not response or not response.webpage:
            self.download_error('Unable to get %s date %s', self.baseurl, dateobj)
            return dls

        curr_url = response.response_url

        postdata = self.get_form_data(response.webpage, dateobj)
        if postdata is None:
            self.download_error('Unable to retreive form data for date %s', dateobj)
            return dls
        postdata.append(('BtnSearch', 'Search'))

        searchurl = urllib.parse.urljoin(curr_url, self.search_endp)

        response = self.download_url(searchurl, postdata = postdata, referer = curr_url, \
                                     loadcookies = cookiejar, savecookies = cookiejar)
        pagenum = 1
        while response is not None and response.webpage is not None:
            curr_url = response.response_url

            metainfos, nextpage = self.parse_search_results(response.webpage, \
                                                            dateobj, pagenum)

            postdata = self.get_form_data(response.webpage, dateobj)
            relurls = self.download_metainfos(relpath, metainfos, curr_url, \
                                              postdata, cookiejar)
            dls.extend(relurls)
            if not nextpage:
                break

            pagenum += 1
            self.logger.info('Going to page %d for date %s', pagenum, dateobj)
            response = self.download_nextpage(nextpage, curr_url, postdata, cookiejar)
        else:
            self.download_error('Unable to fetch result page for %s', dateobj)

        return dls
