# Adapted from egazette/utils/download.py for egovorder.
# Modified for the GOIR-only extraction; see LICENSE for licensing scope.

import multiprocessing
import urllib.request, urllib.error, urllib.parse
import time
import logging

from . import utils

def sync(hostname, sourceobjs, fromdate, todate, event):
    proxydict = utils.get_proxydict(hostname)
    if proxydict is not None:
        proxy = urllib.request.ProxyHandler(proxydict)
        opener = urllib.request.build_opener(proxy)
        urllib.request.install_opener(opener)

    for obj in sourceobjs:
        if fromdate == None and todate == None:
            obj.sync_daily(event)
        else:
            obj.sync(fromdate, todate, event)

def all_downloads(hostname, sourceobjs, event):
    for obj in sourceobjs:
        obj.all_downloads(event)

def agg_host_processes(sourceobjs, all_dls, fromdate, todate, event):
    srcdict = {}
    for src in sourceobjs:
        hostname = src.hostname
        if not (hostname in srcdict):
            srcdict[hostname] = []
        srcdict[hostname].append(src)

    tlist = []
    for hostname, srclist in srcdict.items():
        if all_dls:
            t = multiprocessing.Process(target = all_downloads, args = (hostname, srclist, event))
        else:
            t = multiprocessing.Process(target = sync, args = \
                                (hostname, srclist, fromdate, todate, event))
        t.start()
        tlist.append(t)

    return tlist

def noagg_host_processes(sourceobjs, all_dls, fromdate, todate, event):
    tlist = []
    for src in sourceobjs:
        if all_dls:
            t = multiprocessing.Process(target = all_downloads, args = (src.hostname, [src], event))
        else:
            t = multiprocessing.Process(target = sync, args = \
                                (src.hostname, [src], fromdate, todate, event))
        t.start()
        tlist.append(t)

    return tlist

def parallel_download(sourceobjs, agghosts, fromdate, todate, max_wait, all_dls):
    event = multiprocessing.Event()
    if agghosts:
        tlist = agg_host_processes(sourceobjs, all_dls, fromdate, todate, event)
    else:
        tlist = noagg_host_processes(sourceobjs, all_dls, fromdate, todate, event)

    start_ts = time.time()
    for t in tlist:
        if max_wait != None and max_wait <= 5:
            break

        t.join(max_wait)
        end_ts    = time.time()
        if max_wait != None:
            elapsed   = end_ts - start_ts
            max_wait -= elapsed
            start_ts  = end_ts

    if max_wait is not None and any(t.is_alive() for t in tlist):
        logger = logging.getLogger('crawler.controller')
        logger.warning('Time expired. Setting the event and asking the crawlers to exit')
        event.set()
        for t in tlist:
            if t.is_alive():
                t.join()

    return all(t.exitcode == 0 for t in tlist)
