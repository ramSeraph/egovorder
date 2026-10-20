# Adapted from egazette/iasync.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

import logging
import os
import re
import shutil
import time

from requests.exceptions import HTTPError
from internetarchive import upload, get_session, get_item, modify_metadata

from .srcs import datasrcs_info
from .utils import pdf_ops


class Stats:
    def __init__(self):
        self.uploads = {}
        self.upload_success = {}

        self.modify = {}
        self.modify_success = {}

    def update_upload(self, srcname, success):
        self.update(srcname, success, self.uploads, self.upload_success)

    def update_modify(self, srcname, success):
        self.update(srcname, success, self.modify, self.modify_success)

    def update(self, srcname, success, total, total_success):
        if srcname not in total:
            total[srcname]         = 0
            total_success[srcname] = 0

        total[srcname] += 1

        if success:
            total_success[srcname] += 1

    def get_msg_by_srcs(self, msg, total, total_success):
        msg.append('------------')
        msg.append('Srcname\tTotal\tSuccess')
        keys = list(total.keys())
        keys.sort()
        for srcname in keys:
            msg.append('%s\t%d\t%d' % (srcname, total[srcname], total_success[srcname]))
        msg.append('\n')

    def get_message(self, srcnames):
        msg = []
        if self.uploads:
            msg.append('Upload Stats')
            self.get_msg_by_srcs(msg, self.uploads, self.upload_success)

        if self.modify:
            msg.append('Modify Stats')
            self.get_msg_by_srcs(msg, self.modify, self.modify_success)

        noupdate = []
        for src in srcnames:
            if src not in self.uploads and src not in self.modify:
                noupdate.append(src)
        if noupdate:
             msg.append('No updates from %s' % ', '.join(noupdate))
        return '\n'.join(msg)

class OrderIA:
    def __init__(self, file_storage, access_key, secret_key, \
                 loglevel, logfile, to_sandbox):
        self.file_storage = file_storage
        self.access_key   = access_key
        self.secret_key   = secret_key
        self.to_sandbox    = to_sandbox

        session_data = {'access': access_key, 'secret': secret_key}
        if logfile:
            logconfig    = {'logging': {'level': loglevel, 'file': logfile}}
        else:
            logconfig    = {'logging': {'level': loglevel}}

        self.session = get_session({'s3': session_data, 'logging': logconfig})
        self.logger = logging.getLogger('iasync')

        self.num_upload_retries = 100
        self.num_reattempts = 5
        self.reattempt_delay_secs = 300

    def get_ia_item(self, identifier):
        try:
            item = get_item(identifier, archive_session = self.session)
        except Exception as e:
            self.logger.warning('Could not get item %s. Error %s' , identifier, e)
            item = None
        return item

    def get_identifier(self, relurl, metainfo):
        return datasrcs_info.get_identifier(relurl, metainfo, self.to_sandbox)

    def upload(self, relurl):
        metainfo = self.file_storage.get_metainfo(relurl)
        if metainfo == None:
            self.logger.warning('No metainfo, Ignoring upload for %s' % relurl)
            return False

        identifier = self.get_identifier(relurl, metainfo)
        if identifier == None:
            self.logger.warning('Could not form IA identifier. Ignoring upload for %s' % relurl)
            return False

        while 1:
            item = self.get_ia_item(identifier)
            if item:
                break
            time.sleep(self.reattempt_delay_secs)

        rawfile  = self.file_storage.get_rawfile_path(relurl)
        metafile = self.file_storage.get_metafile_path(relurl)
        if not rawfile or not metafile:
            self.logger.warning('Missing local file or metadata for %s', relurl)
            return False

        if item.exists:
            filelist = item.get_files()

            files = set([f.name for f in filelist])
            rawname  = rawfile.split('/')[-1]
            metaname = metafile.split('/')[-1]

            to_upload = []
            if rawname in files:
                self.logger.info('Rawfile already exists for %s. Ignoring.' % \
                                 relurl)
            else:
                to_upload.append(rawfile)

            if metaname in files:
                self.logger.info('Metafile already exists for %s. Ignoring.' % \
                                 relurl)
            else:
                to_upload.append(metafile)
            metadata = None
        else:
            files = set([])
            metadata  = self.to_ia_metadata(relurl, metainfo)
            to_upload = [rawfile, metafile]

        if not to_upload:
            self.logger.info('No files need to be uploaded for %s', identifier)
            return True

        success = self.ia_upload(identifier, metadata, to_upload, files)

        if success:
            self.logger.info('Successfully uploaded %s', identifier)
        else:
            self.logger.warning('Error in uploading %s', identifier)

        return success

    def pop_rawfile(self, to_upload):
        idx = -1
        for i,file in enumerate(to_upload):
            if file.endswith('.pdf'):
                idx = i
                break

        if idx == -1:
            return None

        return to_upload.pop(idx)

    def ia_upload(self, identifier, metadata, to_upload, files):
        uploaded = False
        bad_pdf_detected = False
        to_del = []

        count = self.num_reattempts
        while count > 0:
            try:
                if metadata:
                    upload(identifier, to_upload, metadata = metadata, \
                           access_key = self.access_key, \
                           secret_key = self.secret_key, \
                           retries=self.num_upload_retries)
                else:
                    upload(identifier, to_upload, \
                           access_key = self.access_key, \
                           secret_key = self.secret_key, \
                           retries=self.num_upload_retries)
                uploaded = True
                break
            except HTTPError as e:
                self.logger.warning('Error in upload for %s: %s', identifier, e)

                msg = str(e)
                if re.search('Syntax error detected in pdf data', msg) or \
                       re.search('error checking pdf file', msg):
                    if bad_pdf_detected:
                        self.logger.warning('Already attempted fixing bad pdf, giving up for %s: %s', \
                                            identifier, e)
                        break
                    bad_pdf_detected = True

                    rawfile = self.pop_rawfile(to_upload)

                    if rawfile is None:
                        self.logger.error('Unable to locate the bad pdf for %s', identifier)
                        break

                    renamed_pdf_file = self.rename_bad_pdf(rawfile)
                    to_del.append(renamed_pdf_file)
                    if renamed_pdf_file in files:
                        self.logger.warning('Renamed PDF file already exists. Ignoring. for %s', identifier)
                    else:
                        to_upload.append(renamed_pdf_file)

                    corrected_pdf_file = self.create_corrected_pdf(rawfile)
                    if corrected_pdf_file is not None:
                        to_upload.append(corrected_pdf_file)
                        to_del.append(corrected_pdf_file)
                    continue

                elif re.search('pdf requires a password', msg):

                    rawfile = self.pop_rawfile(to_upload)

                    if rawfile is None:
                        self.logger.error('Unable to locate the bad locked pdf for %s', identifier)
                        break

                    renamed_pdf_file = self.rename_bad_pdf(rawfile)
                    to_del.append(renamed_pdf_file)
                    if renamed_pdf_file in files:
                        self.logger.warning('Renamed PDF file already exists. Ignoring. for %s', identifier)
                    else:
                        to_upload.append(renamed_pdf_file)
                    continue

            except Exception as e:
                self.logger.warning('Error in upload for %s: %s', identifier, e)

            count = count - 1
            time.sleep(self.reattempt_delay_secs)

        for file in to_del:
            os.remove(file)

        return uploaded

    def rename_bad_pdf(self, rawfile):
        name = '%s-' %rawfile.split('/')[-1]
        tmpfile = '/tmp/%s' % name
        shutil.copyfile(rawfile, tmpfile)
        return tmpfile

    def create_corrected_pdf(self, rawfile):
        name = '%s' %rawfile.split('/')[-1]

        tmpfile = '/tmp/%s' % name

        try:
            pdf_ops.convert_to_image_pdf_file(rawfile, tmpfile)
            return tmpfile
        except Exception as ex:
            self.logger.error('Unable to convert unacceptble pdf file to image pdf file, ex: %s', ex)
            return None

    def get_srcname(self, relurl):
       words    = relurl.split('/')
       return words[0]

    def to_ia_metadata(self, relurl, metainfo):
        src      = self.get_srcname(relurl)
        srcinfo  = datasrcs_info.srcinfos[src]

        return metainfo.get_ia_metadata(srcinfo, self.to_sandbox)

    def update_meta(self, relurl, update_meta_file):
        metainfo = self.file_storage.get_metainfo(relurl)
        if metainfo == None:
            self.logger.warning('No metainfo, Ignoring upload for %s' % relurl)
            return False

        identifier = self.get_identifier(relurl, metainfo)

        while 1:
            item = self.get_ia_item(identifier)
            if item:
                break
            time.sleep(self.reattempt_delay_secs)

        if not item.exists:
            return self.upload(relurl)
        else:
            metadata = self.to_ia_metadata(relurl, metainfo)
            while 1:
                if self.ia_modify_metadat(identifier, metadata):
                    break
                time.sleep(self.reattempt_delay_secs)

            if update_meta_file:
                metafile = self.file_storage.get_metafile_path(relurl)
                upload(identifier, [metafile], \
                       access_key = self.access_key, \
                       secret_key = self.secret_key, \
                       retries=self.num_upload_retries)
            self.logger.info('Updated metadata for %s', identifier)

        return True

    def ia_modify_metadat(self, identifier, metadata):
        try:
            modify_metadata(identifier, metadata = metadata, \
                            access_key = self.access_key, \
                            secret_key = self.secret_key)
        except Exception as e:
            self.logger.warning('Could not  modify metadata %s. Error %s' , identifier, e)
            return False
        return True
