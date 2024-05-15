# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

from asyncio import Task, sleep, get_running_loop, as_completed
from os import path, stat, remove, makedirs
from random import uniform as frand
from typing import Optional, List, Dict

from aiofile import async_open
from aiohttp import ClientSession, ClientPayloadError

from config import Config
from defs import (
    Mem, NamingFlags, DownloadResult, CONNECT_RETRIES_BASE, SITE_AJAX_REQUEST_VIDEO, DOWNLOAD_POLICY_ALWAYS, DOWNLOAD_MODE_TOUCH, PREFIX,
    DOWNLOAD_MODE_SKIP, TAGS_CONCAT_CHAR, SITE, SCREENSHOTS_COUNT,
    FULLPATH_MAX_BASE_LEN, CONNECT_REQUEST_DELAY,
)
from downloader import VideoDownloadWorker
from dscanner import VideoScanWorker
from dthrottler import ThrottleChecker
from fetch_html import fetch_html, wrap_request, make_session
from logger import Log
from path_util import file_already_exists, try_rename
from rex import re_media_filename
from scenario import DownloadScenario
from tagger import filtered_tags, is_filtered_out_by_extra_tags
from util import has_naming_flag, format_time, get_elapsed_time_i, extract_ext
from vinfo import VideoInfo, export_video_info, get_min_max_ids

__all__ = ('download', 'at_interrupt')


async def download(sequence: List[VideoInfo], by_id: bool, filtered_count: int, session: ClientSession = None) -> None:
    minid, maxid = get_min_max_ids(sequence)
    eta_min = int(2.0 + (CONNECT_REQUEST_DELAY + 0.2 + 0.02) * len(sequence))
    Log.info(f'\nOk! {len(sequence):d} ids (+{filtered_count:d} filtered out), bound {minid:d} to {maxid:d}. Working...\n'
             f'\nThis will take at least {eta_min:d} seconds{f" ({format_time(eta_min)})" if eta_min >= 60 else ""}!\n')
    async with session or make_session() as session:
        if by_id:
            for cv in as_completed([
                VideoScanWorker(sequence, scan_video).run(),
                VideoDownloadWorker(sequence, process_video, filtered_count, session).run()
            ]):
                await cv
        else:
            await VideoDownloadWorker(sequence, download_video, filtered_count, session).run()
    export_video_info(sequence)


async def scan_video(vi: VideoInfo) -> DownloadResult:
    dwn = VideoDownloadWorker.get()
    scn = VideoScanWorker.get()
    scenario = Config.scenario  # type: Optional[DownloadScenario]
    sname = vi.sname
    extra_ids = scn.get_extra_ids() if scn else []  # type: List[int]
    my_tags = 'no_tags'
    rating = vi.rating
    score = ''

    vi.set_state(VideoInfo.State.SCANNING)
    a_html = await fetch_html(f'{SITE_AJAX_REQUEST_VIDEO % vi.id}?popup_id={2 + vi.id % 10:d}', session=dwn.session)
    if a_html is None:
        Log.error(f'Error: unable to retreive html for {sname}! Aborted!')
        return DownloadResult.FAIL_RETRIES

    if a_html.find('title', string='404 Not Found'):
        Log.error(f'Got error 404 for {sname}, skipping...')
        return DownloadResult.FAIL_NOT_FOUND

    if not vi.title:
        titleh1 = a_html.find('h1', class_='title_video')
        vi.title = titleh1.text if titleh1 else ''
    try:
        dislikes_int = 0
        likes_int = int(a_html.find('span', class_='voters count').text.replace(' likes', '').replace(' like', ''))
        rating = f'{(likes_int * 100) // (dislikes_int + likes_int):d}' if (dislikes_int + likes_int) > 999999 else rating
        score = f'{likes_int - dislikes_int:d}'
    except Exception:
        Log.warn(f'Warning: cannot extract score for {sname}.')
    try:
        my_authors = [str(a.string).lower() for a in a_html.find('div', string='Artist:').parent.find_all('span')]
    except Exception:
        Log.warn(f'Warning: cannot extract authors for {sname}.')
        my_authors = list()
    try:
        my_categories = [str(c.string).lower() for c in a_html.find('div', string='Categories:').parent.find_all('span')]
    except Exception:
        Log.warn(f'Warning: cannot extract categories for {sname}.')
        my_categories = list()
    tdiv = a_html.find('div', string='Tags:')
    if tdiv is None:
        Log.info(f'Warning: video {sname} has no tags!')
    tags = [str(elem.string) for elem in tdiv.parent.find_all('a', class_='tag_item')] if tdiv else ['']
    tags_raw = [tag.replace(' ', '_').lower() for tag in tags if len(tag) > 0]
    for add_tag in [ca.replace(' ', '_') for ca in my_categories + my_authors if len(ca) > 0]:
        if add_tag not in tags_raw:
            tags_raw.append(add_tag)
    if is_filtered_out_by_extra_tags(vi, tags_raw, Config.extra_tags, Config.id_sequence, vi.subfolder, extra_ids):
        Log.info(f'Info: video {sname} is filtered out by{" outer" if scenario is not None else ""} extra tags, skipping...')
        return DownloadResult.FAIL_SKIPPED
    for vsrs, csri, srn, pc in zip((score, rating), (Config.min_score, Config.min_rating), ('score', 'rating'), ('', '%')):
        if len(vsrs) > 0 and csri is not None:
            try:
                if int(vsrs) < csri:
                    Log.info(f'Info: video {sname} has low {srn} \'{vsrs}{pc}\' (required {csri:d}), skipping...')
                    return DownloadResult.FAIL_SKIPPED
            except Exception:
                pass
    if scenario is not None:
        matching_sq = scenario.get_matching_subquery(vi, tags_raw, score, rating)
        utpalways_sq = scenario.get_utp_always_subquery() if tdiv is None else None
        if matching_sq:
            vi.subfolder = matching_sq.subfolder
            vi.quality = matching_sq.quality
        elif utpalways_sq:
            vi.subfolder = utpalways_sq.subfolder
            vi.quality = utpalways_sq.quality
        else:
            Log.info(f'Info: unable to find matching or utp scenario subquery for {sname}, skipping...')
            return DownloadResult.FAIL_SKIPPED
    elif tdiv is None and len(Config.extra_tags) > 0 and Config.utp != DOWNLOAD_POLICY_ALWAYS:
        Log.warn(f'Warning: could not extract tags from {sname}, skipping due to untagged videos download policy...')
        return DownloadResult.FAIL_SKIPPED
    if Config.save_tags:
        vi.tags = ' '.join(sorted(tags_raw))
    if Config.save_descriptions or Config.save_comments:
        cidivs = a_html.find_all('div', class_='comment-info')
        cudivs = [cidiv.find('a') for cidiv in cidivs]
        ctdivs = [cidiv.find('div', class_='coment-text') for cidiv in cidivs]
        desc_em = a_html.find('em')  # exactly one
        uploader_div = a_html.find('div', string=' Uploaded By: ')
        my_uploader = uploader_div.parent.find('a', class_='name').text.lower().strip() if uploader_div else 'unknown'
        has_description = (cudivs[-1].text.lower() == my_uploader) if (cudivs and ctdivs) else False  # first comment by uploader
        if cudivs and ctdivs:
            assert len(ctdivs) == len(cudivs)
        if Config.save_descriptions:
            desc_comment = (f'{cudivs[-1].text}:\n' + ctdivs[-1].get_text('\n').strip()) if has_description else ''
            desc_base = (f'\n{my_uploader}:\n' + desc_em.get_text('\n') + '\n') if desc_em else ''
            vi.description = desc_base or (f'\n{desc_comment}\n' if desc_comment else '')
        if Config.save_comments:
            comments_list = [f'{cudivs[i].text}:\n' + ctdivs[i].get_text('\n').strip() for i in range(len(ctdivs) - int(has_description))]
            vi.comments = ('\n' + '\n\n'.join(comments_list) + '\n') if comments_list else ''
    my_tags = filtered_tags(sorted(tags_raw)) or my_tags

    tries = 0
    while True:
        ddiv = a_html.find('div', string='Download:')
        if ddiv is not None and ddiv.parent is not None:
            break
        message_span = a_html.find('span', class_='message')
        if message_span:
            Log.warn(f'Cannot find download section for {sname}, reason: \'{message_span.text}\', skipping...')
            return DownloadResult.FAIL_SKIPPED
        elif tries >= 5:
            Log.error(f'Cannot find download section for {sname} after {tries:d} tries, failed!')
            return DownloadResult.FAIL_RETRIES
        tries += 1
        Log.debug(f'No download section for {sname}, retry #{tries:d}...')
        a_html = await fetch_html(f'{SITE_AJAX_REQUEST_VIDEO % vi.id}?popup_id={2 + tries + vi.id % 10:d}', session=dwn.session)
    links = ddiv.parent.find_all('a', class_='tag_item')
    qualities = list()
    for lin in links:
        try:
            qualities.append(lin.text.replace('MP4 ', ''))
        except Exception:
            pass
    if vi.quality not in qualities:
        q_idx = 0
        Log.warn(f'Warning: cannot find quality \'{vi.quality}\' for {sname}, selecting \'{qualities[q_idx]}\'')
        vi.quality = qualities[q_idx]
        link_idx = q_idx
    else:
        link_idx = qualities.index(vi.quality)
    vi.link = links[link_idx].get('href')

    rv_ = PREFIX if has_naming_flag(NamingFlags.PREFIX) else ''
    fname_part2 = extract_ext(vi.link)
    my_score = (f'{f"+" if score.isnumeric() else ""}{score}' if len(score) > 0
                else '' if len(rating) > 0 else 'unk')
    my_rating = (f'{", " if  len(my_score) > 0 else ""}{rating}{"%" if rating.isnumeric() else ""}' if len(rating) > 0
                 else '' if len(my_score) > 0 else 'unk')
    fname_part1 = (
        f'{rv_}{vi.id:d}'
        f'{f"_({my_score}{my_rating})" if has_naming_flag(NamingFlags.SCORE) else ""}'
        f'{f"_{vi.title}" if vi.title and has_naming_flag(NamingFlags.TITLE) else ""}'
    )
    # <fname_part1>_(<TAGS...>)_<QUALITY><fname_part2>
    extra_len = 2 + 2 + 5  # 2 underscores + 2 brackets + len('2160p') - max len of all qualities
    if has_naming_flag(NamingFlags.TAGS):
        while len(my_tags) > max(0, FULLPATH_MAX_BASE_LEN - (len(vi.my_folder) + len(fname_part1) + len(fname_part2) + extra_len)):
            my_tags = my_tags[:max(0, my_tags.rfind(TAGS_CONCAT_CHAR))]
        fname_part1 += f'_({my_tags})' if len(my_tags) > 0 else ''
    if len(my_tags) == 0 and len(fname_part1) > max(0, FULLPATH_MAX_BASE_LEN - (len(vi.my_folder) + len(fname_part2) + extra_len)):
        fname_part1 = fname_part1[:max(0, FULLPATH_MAX_BASE_LEN - (len(vi.my_folder) + len(fname_part2) + extra_len))]
    fname_part1 = fname_part1.strip()

    fname_mid = f'_{vi.quality}' if has_naming_flag(NamingFlags.QUALITY) else ''
    vi.filename = f'{fname_part1}{fname_mid}{fname_part2}'

    vi.set_state(VideoInfo.State.SCANNED)
    return DownloadResult.SUCCESS


async def process_video(vi: VideoInfo) -> DownloadResult:
    vi.set_state(VideoInfo.State.ACTIVE)
    res = await download_video(vi)
    if res not in (DownloadResult.SUCCESS, DownloadResult.FAIL_SKIPPED, DownloadResult.FAIL_ALREADY_EXISTS):
        vi.set_state(VideoInfo.State.FAILED)
    return res


async def download_sceenshot(vi: VideoInfo, scr_num: int) -> DownloadResult:
    dwn = VideoDownloadWorker.get()
    sname = f'{PREFIX}{vi.id:d}_{scr_num:02d}.webp'
    sfilename = f'{f"{vi.subfolder}/" if len(vi.subfolder) > 0 else ""}{PREFIX}{vi.id:d}/{scr_num:02d}.webp'
    my_folder = f'{vi.my_folder}{PREFIX}{vi.id:d}/'
    fullpath = f'{my_folder}{scr_num:02d}.webp'
    my_link = f'{SITE}/contents/videos_screenshots/{vi.id - vi.id % 1000:d}/{vi.id:d}/336x189/{scr_num:d}.jpg'
    ret = DownloadResult.SUCCESS

    if not path.isdir(my_folder):
        try:
            makedirs(my_folder)
        except Exception:
            raise IOError(f'ERROR: Unable to create subfolder \'{my_folder}\'!')

    try:
        async with await wrap_request(dwn.session, 'GET', my_link) as r:
            if r.status == 404:
                Log.error(f'Got 404 for {sname}...!')
                ret = DownloadResult.FAIL_NOT_FOUND
            elif r.content_type and 'text' in r.content_type:
                Log.error(f'File not found at {my_link}!')
                ret = DownloadResult.FAIL_NOT_FOUND

            expected_size = r.content_length
            async with async_open(fullpath, 'wb') as outf:
                async for chunk in r.content.iter_chunked(4 * Mem.MB):
                    await outf.write(chunk)

            file_size = stat(fullpath).st_size
            if expected_size and file_size != expected_size:
                Log.error(f'Error: file size mismatch for {sfilename}: {file_size:d} / {expected_size:d}')
                ret = DownloadResult.FAIL_RETRIES
    except Exception:
        ret = DownloadResult.FAIL_NOT_FOUND

    return ret


async def download_sceenshots(vi: VideoInfo) -> DownloadResult:
    ret = DownloadResult.SUCCESS
    for t in [get_running_loop().create_task(download_sceenshot(vi, scr_idx + 1))
              for scr_idx in range(SCREENSHOTS_COUNT)]:  # type: Task[DownloadResult]
        res = await t
        if res not in (DownloadResult.SUCCESS, ret):
            ret = res
    return ret


async def download_video(vi: VideoInfo) -> DownloadResult:
    dwn = VideoDownloadWorker.get()
    retries = 0
    ret = DownloadResult.SUCCESS
    skip = Config.dm == DOWNLOAD_MODE_SKIP
    status_checker = ThrottleChecker(vi)

    if skip is True:
        vi.set_state(VideoInfo.State.DONE)
        ret = DownloadResult.FAIL_SKIPPED
    else:
        vi.set_state(VideoInfo.State.DOWNLOADING)
        if not path.isdir(vi.my_folder):
            try:
                makedirs(vi.my_folder)
            except Exception:
                raise IOError(f'ERROR: Unable to create subfolder \'{vi.my_folder}\'!')
        else:
            rv_match = re_media_filename.match(vi.filename)
            rv_quality = rv_match.group(2)
            rv_curfile = file_already_exists(vi.id, rv_quality)
            if rv_curfile:
                exact_name = rv_curfile == vi.my_fullpath
                vi.set_flag(VideoInfo.Flags.ALREADY_EXISTED_EXACT if exact_name else VideoInfo.Flags.ALREADY_EXISTED_SIMILAR)
                if Config.continue_mode:
                    if not exact_name:
                        old_filename = path.split(rv_curfile)[1]
                        Log.info(f'{vi.sffilename} {vi.quality} (or similar) found. Enforcing new name (was \'{old_filename}\').')
                        if not try_rename(rv_curfile, vi.my_fullpath):
                            Log.warn(f'Warning: file {vi.sffilename} already exists! Old file will be preserved.')
                else:
                    Log.info(f'{vi.sffilename} (or similar) already exists. Skipped.')
                    vi.set_state(VideoInfo.State.DONE)
                    return DownloadResult.FAIL_ALREADY_EXISTS

    while (not skip) and retries < CONNECT_RETRIES_BASE:
        try:
            file_exists = path.isfile(vi.my_fullpath)
            if file_exists and retries == 0:
                vi.set_flag(VideoInfo.Flags.ALREADY_EXISTED_EXACT)
            file_size = stat(vi.my_fullpath).st_size if file_exists else 0

            if Config.dm == DOWNLOAD_MODE_TOUCH:
                if file_exists:
                    Log.info(f'{vi.sfsname} ({vi.quality}) already exists, size: {file_size:d} ({file_size / Mem.MB:.2f} Mb)')
                    vi.set_state(VideoInfo.State.DONE)
                    return DownloadResult.FAIL_ALREADY_EXISTS
                else:
                    Log.info(f'Saving<touch> {vi.sname} {0.0:.2f} Mb to {vi.sffilename}')
                    with open(vi.my_fullpath, 'wb'):
                        vi.set_flag(VideoInfo.Flags.FILE_WAS_CREATED)
                        vi.set_state(VideoInfo.State.DONE)
                break

            hkwargs = {'headers': {'Range': f'bytes={file_size:d}-'}} if file_size > 0 else {}  # type: Dict[str, Dict[str, str]]
            r = None
            async with await wrap_request(dwn.session, 'GET', vi.link, **hkwargs) as r:
                content_len = r.content_length or 0
                content_range_s = r.headers.get('Content-Range', '/').split('/', 1)
                content_range = int(content_range_s[1]) if len(content_range_s) > 1 and content_range_s[1].isnumeric() else 1
                if (content_len == 0 or r.status == 416) and file_size >= content_range:
                    Log.warn(f'{vi.sfsname} ({vi.quality}) is already completed, size: {file_size:d} ({file_size / Mem.MB:.2f} Mb)')
                    vi.set_state(VideoInfo.State.DONE)
                    ret = DownloadResult.FAIL_ALREADY_EXISTS
                    break
                if r.status == 404:
                    Log.error(f'Got 404 for {vi.sfsname}...!')
                    retries = CONNECT_RETRIES_BASE - 1
                    ret = DownloadResult.FAIL_NOT_FOUND
                if r.content_type and 'text' in r.content_type:
                    Log.error(f'File not found at {vi.link}!')
                    raise FileNotFoundError(vi.link)

                status_checker.prepare(r, file_size)
                vi.expected_size = file_size + content_len
                vi.last_check_size = vi.start_size = file_size
                vi.last_check_time = vi.start_time = get_elapsed_time_i()
                starting_str = f' <continuing at {file_size:d}>' if file_size else ''
                total_str = f' / {vi.expected_size / Mem.MB:.2f}' if file_size else ''
                Log.info(f'Saving{starting_str} {vi.sname} {content_len / Mem.MB:.2f}{total_str} Mb to {vi.sffilename}')

                dwn.add_to_writes(vi)
                vi.set_state(VideoInfo.State.WRITING)
                status_checker.run()
                async with async_open(vi.my_fullpath, 'ab') as outf:
                    vi.set_flag(VideoInfo.Flags.FILE_WAS_CREATED)
                    async for chunk in r.content.iter_chunked(1 * Mem.MB):
                        await outf.write(chunk)
                status_checker.reset()
                dwn.remove_from_writes(vi)

                file_size = stat(vi.my_fullpath).st_size
                if vi.expected_size and file_size != vi.expected_size:
                    Log.error(f'Error: file size mismatch for {vi.sfsname}: {file_size:d} / {vi.expected_size:d}')
                    raise IOError(vi.link)

                vi.set_state(VideoInfo.State.DONE)
                break
        except Exception as e:
            import sys
            print(sys.exc_info()[0], sys.exc_info()[1])
            if (r is None or r.status != 403) and isinstance(e, ClientPayloadError) is False:
                retries += 1
                Log.error(f'{vi.sffilename}: error #{retries:d}...')
            if r is not None and r.closed is False:
                r.close()
            # Network error may be thrown before item is added to active downloads
            if dwn.is_writing(vi):
                dwn.remove_from_writes(vi)
            status_checker.reset()
            if retries < CONNECT_RETRIES_BASE:
                vi.set_state(VideoInfo.State.DOWNLOADING)
                await sleep(frand(1.0, 7.0))
            elif Config.keep_unfinished is False and path.isfile(vi.my_fullpath) and vi.has_flag(VideoInfo.Flags.FILE_WAS_CREATED):
                Log.error(f'Failed to download {vi.sffilename}. Removing unfinished file...')
                remove(vi.my_fullpath)

    ret = (ret if ret in (DownloadResult.FAIL_NOT_FOUND, DownloadResult.FAIL_SKIPPED, DownloadResult.FAIL_ALREADY_EXISTS) else
           DownloadResult.SUCCESS if retries < CONNECT_RETRIES_BASE else
           DownloadResult.FAIL_RETRIES)

    if Config.save_screenshots:
        sret = await download_sceenshots(vi)
        if sret != DownloadResult.SUCCESS:
            Log.warn(f'{vi.sffilename}: `download_sceenshots()` has failed items (ret = {str(sret)})')

    return ret


def at_interrupt() -> None:
    dwn = VideoDownloadWorker.get()
    if dwn is not None:
        return dwn.at_interrupt()

#
#
#########################################
