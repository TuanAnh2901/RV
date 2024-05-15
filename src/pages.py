# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys
from asyncio import run as run_async, sleep
from typing import Sequence

from cmdargs import HelpPrintExitException, prepare_arglist
from config import Config
from defs import (
    PREFIX, SITE_AJAX_REQUEST_SEARCH_PAGE, SITE_AJAX_REQUEST_UPLOADER_PAGE, SITE_AJAX_REQUEST_PLAYLIST_PAGE, SITE_AJAX_REQUEST_MODEL_PAGE,
    QUALITIES, NamingFlags,
)
from download import download, at_interrupt
from fetch_html import make_session, fetch_html
from logger import Log
from path_util import prefilter_existing_items
from rex import re_page_entry, re_paginator, re_preview_entry
from util import at_startup, has_naming_flag
from validators import find_and_resolve_config_conflicts
from vinfo import VideoInfo

__all__ = ('main_sync',)


async def main(args: Sequence[str]) -> None:
    try:
        arglist = prepare_arglist(args, True)
    except HelpPrintExitException:
        return

    Config.read(arglist, True)

    full_download = Config.quality != QUALITIES[-1]
    video_ref_class = 'th' if Config.playlist_name else 'th js-open-popup'

    if find_and_resolve_config_conflicts(full_download) is True:
        await sleep(3.0)

    def check_id_bounds(video_id: int) -> bool:
        if video_id > Config.end_id:
            Log.trace(f'skipping {video_id:d} > {Config.end_id:d}')
            return False
        if video_id < Config.start_id:
            Log.trace(f'skipping {video_id:d} < {Config.start_id:d}')
            return False
        return True

    v_entries = list()
    maxpage = Config.end if Config.start == Config.end else 0

    pi = Config.start
    async with make_session() as s:
        while pi <= Config.end:
            if pi > maxpage > 0:
                Log.info('reached parsed max page, page scan completed')
                break

            page_addr = (
                (SITE_AJAX_REQUEST_PLAYLIST_PAGE % (Config.playlist_id, Config.playlist_name, pi)) if Config.playlist_name else
                (SITE_AJAX_REQUEST_UPLOADER_PAGE % (Config.uploader, pi)) if Config.uploader else
                (SITE_AJAX_REQUEST_MODEL_PAGE % (Config.model, pi)) if Config.model else
                (SITE_AJAX_REQUEST_SEARCH_PAGE % (Config.search_tags, Config.search_arts, Config.search_cats, Config.search, pi))
            )
            a_html = await fetch_html(page_addr, session=s)
            if not a_html:
                Log.error(f'Error: cannot get html for page {pi:d}')
                continue

            pi += 1

            if maxpage == 0:
                for page_ajax in a_html.find_all('a', attrs={'data-action': 'ajax'}):
                    try:
                        maxpage = max(maxpage, int(re_paginator.search(str(page_ajax.get('data-parameters'))).group(1)))
                    except Exception:
                        pass
                if maxpage == 0:
                    Log.info('Could not extract max page, assuming single page search')
                    maxpage = 1
                else:
                    Log.debug(f'Extracted max page: {maxpage:d}')

            if Config.get_maxid:
                miref = a_html.find('a', class_=video_ref_class)
                max_id = re_page_entry.search(str(miref.get('href'))).group(1)
                Log.fatal(f'{PREFIX[:2].upper()}: {max_id}')
                return

            Log.info(f'page {pi - 1:d}...{" (this is the last page!)" if (0 < maxpage == pi - 1) else ""}')

            if full_download:
                arefs = a_html.find_all('a', class_=video_ref_class)
                for aref in arefs:
                    cur_id = int(re_page_entry.search(str(aref.get('href'))).group(1))
                    if check_id_bounds(cur_id) is False:
                        continue
                    elif cur_id in v_entries:
                        Log.warn(f'Warning: id {cur_id:d} already queued, skipping')
                        continue
                    my_title = str(aref.get('title', ''))
                    v_entries.append(VideoInfo(cur_id, my_title))
            else:
                content_div = a_html.find('div', class_='thumbs clearfix')

                if content_div is None:
                    Log.error(f'Error: cannot get content div for page {pi:d}')
                    continue

                prev_all = content_div.find_all('div', class_='img wrap_image')
                titl_all = content_div.find_all('div', class_='thumb_title')
                for i, p in enumerate(prev_all):
                    link = str(p.get('data-preview'))
                    title = str(titl_all[i].text)
                    v_id = re_preview_entry.search(link)
                    cur_id, cur_ext = int(v_id.group(1)), str(v_id.group(2))
                    if check_id_bounds(cur_id) is False:
                        continue
                    elif cur_id in v_entries:
                        Log.warn(f'Warning: id {cur_id:d} already queued, skipping')
                        continue
                    v_entries.append(VideoInfo(
                        cur_id, '', link, '', f'{PREFIX if has_naming_flag(NamingFlags.PREFIX) else ""}{cur_id:d}'
                        f'{f"_{title}" if has_naming_flag(NamingFlags.TITLE) else ""}_preview.{cur_ext}',
                    ))

        v_entries.reverse()
        orig_count = len(v_entries)

        if orig_count > 0:
            prefilter_existing_items(v_entries)

        removed_count = orig_count - len(v_entries)

        if orig_count == removed_count:
            if orig_count > 0:
                Log.fatal(f'\nAll {orig_count:d} videos already exist. Aborted.')
            else:
                Log.fatal('\nNo videos found. Aborted.')
            return

        await download(v_entries, full_download, removed_count, s)


async def run_main(args: Sequence[str]) -> None:
    await main(args)
    await sleep(0.5)


def main_sync(args: Sequence[str]) -> None:
    assert sys.version_info >= (3, 7), 'Minimum python version required is 3.7!'

    try:
        run_async(run_main(args))
    except (KeyboardInterrupt, SystemExit):
        Log.warn('Warning: catched KeyboardInterrupt/SystemExit...')
    finally:
        at_interrupt()


if __name__ == '__main__':
    at_startup()
    main_sync(sys.argv[1:])
    exit(0)

#
#
#########################################
