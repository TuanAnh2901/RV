# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

from asyncio import sleep, as_completed, Queue as AsyncQueue
from os import path, stat, remove, makedirs
from random import uniform as frand
from re import match, search
from typing import List, Optional, Union, Coroutine, Tuple

from aiofile import async_open
from aiohttp import ClientSession, TCPConnector

from defs import (
    CONNECT_RETRIES_ITEM, MAX_VIDEOS_QUEUE_SIZE, TAGS_CONCAT_CHAR, SITE_AJAX_REQUEST_VIDEO,
    DownloadResult, DOWNLOAD_POLICY_ALWAYS, DOWNLOAD_MODE_TOUCH, NamingFlags, calc_sleep_time,
    Log, ExtraConfig, normalize_path, normalize_filename, get_elapsed_time_s, has_naming_flag, prefixp, LoggingFlags, extract_ext,
    re_rvfile,
)
from fetch_html import fetch_html, wrap_request
from path_util import file_exists_in_folder
from scenario import DownloadScenario
from tagger import (
    filtered_tags, get_matching_tag, get_or_group_matching_tag, is_neg_and_group_matches, register_item_tags, dump_item_tags,
)

__all__ = ('DownloadWorker', 'at_interrupt')

download_worker = None  # type: Optional[DownloadWorker]


class DownloadWorker:
    """
    Async queue wrapper which binds list of lists of arguments to a download function call and processes them
    asynchronously with a limit of simulteneous downloads defined by MAX_VIDEOS_QUEUE_SIZE
    """
    params_first_type = Tuple[int, str, Optional[DownloadScenario]]  # download_id
    params_second_type = Tuple[int, str, str, str, Optional[str]]  # download_file
    sequence_type = Tuple[Union[params_first_type, params_second_type]]  # Tuple here makes sure argument is not an empty list

    def __init__(self, my_sequence: sequence_type, by_id: bool, session: ClientSession = None) -> None:
        self._func = download_id if by_id is True else download_file
        self._seq = list(my_sequence)
        self._queue = AsyncQueue(MAX_VIDEOS_QUEUE_SIZE)  # type: AsyncQueue[Tuple[int, Coroutine]]
        self.session = session

        self._downloads_active = []  # type: List[int]
        self.writes_active = []  # type: List[str]
        self.failed_items = []  # type: List[int]

        self._total_queue_size_last = 0
        self._download_queue_size_last = 0
        self._write_queue_size_last = 0

        global download_worker
        assert download_worker is None
        download_worker = self

    async def _at_task_start(self, idi: int) -> None:
        self._downloads_active.append(idi)
        Log.trace(f'[queue] {prefixp()}{idi:d}.mp4 added to queue')

    async def _at_task_finish(self, idi: int) -> None:
        self._downloads_active.remove(idi)
        Log.trace(f'[queue] {prefixp()}{idi:d}.mp4 removed from queue')

    async def _prod(self) -> None:
        while len(self._seq) > 0:
            if self._queue.full() is False:
                await self._queue.put((int(self._seq[0][0]), self._func(*self._seq[0])))
                del self._seq[0]
            else:
                await sleep(0.2)

    async def _cons(self) -> None:
        while len(self._seq) + self._queue.qsize() > 0:
            if self._queue.empty() is False and len(self._downloads_active) < MAX_VIDEOS_QUEUE_SIZE:
                idi, task = await self._queue.get()
                await self._at_task_start(idi)
                await task
                await self._at_task_finish(idi)
                self._queue.task_done()
            else:
                await sleep(0.25)

    async def _report_total_queue_size_callback(self, base_sleep_time: float) -> None:
        while len(self._seq) + self._queue.qsize() + len(self._downloads_active) > 0:
            await sleep(base_sleep_time if len(self._seq) + self._queue.qsize() > 0 else 1.0)
            queue_size = len(self._seq) + self._queue.qsize()
            download_count = len(self._downloads_active)
            write_count = len(self.writes_active)
            queue_last = self._total_queue_size_last
            downloading_last = self._download_queue_size_last
            write_last = self._write_queue_size_last
            if queue_last != queue_size or downloading_last != download_count or write_last != write_count:
                Log.info(f'[{get_elapsed_time_s()}] queue: {queue_size:d}, downloading: {download_count:d} (writing: {write_count:d})')
                self._total_queue_size_last = queue_size
                self._download_queue_size_last = download_count
                self._write_queue_size_last = write_count

    async def _after_download(self) -> None:
        newline = '\n'
        if len(self._seq) != 0:
            Log.fatal(f'total queue is still at {len(self._seq):d} != 0!')
        if len(self.writes_active) > 0:
            Log.fatal(f'active writes count is still at {len(self.writes_active):d} != 0!')
        if len(self.failed_items) > 0:
            Log.fatal(f'Failed items:\n{newline.join(str(fi) for fi in sorted(self.failed_items))}')

    async def run(self) -> None:
        async with self.session or ClientSession(connector=TCPConnector(limit=MAX_VIDEOS_QUEUE_SIZE), read_bufsize=2**20) as self.session:
            for cv in as_completed([self._prod(), self._report_total_queue_size_callback(calc_sleep_time(3.0))] +
                                   [self._cons() for _ in range(MAX_VIDEOS_QUEUE_SIZE)]):
                await cv
        if ExtraConfig.save_tags is True:
            dump_item_tags()
        await self._after_download()


def is_filtered_out_by_extra_tags(idi: int, tags_raw: List[str], extra_tags: List[str], subfolder: str) -> bool:
    suc = True
    if len(extra_tags) > 0:
        for extag in extra_tags:
            if extag[0] == '(':
                if get_or_group_matching_tag(extag, tags_raw) is None:
                    suc = False
                    Log.trace(f'[{subfolder}] Video \'{prefixp()}{idi:d}.mp4\' misses required tag matching \'{extag}\'!',
                              LoggingFlags.LOGGING_EX_MISSING_TAGS)
            elif extag.startswith('-('):
                if is_neg_and_group_matches(extag, tags_raw):
                    suc = False
                    Log.info(f'[{subfolder}] Video \'{prefixp()}{idi:d}.mp4\' contains excluded tags combination \'{extag[1:]}\'!',
                             LoggingFlags.LOGGING_EX_EXCLUDED_TAGS)
            else:
                my_extag = extag[1:] if extag[0] == '-' else extag
                mtag = get_matching_tag(my_extag, tags_raw)
                if mtag is not None and extag[0] == '-':
                    suc = False
                    Log.info(f'[{subfolder}] Video \'{prefixp()}{idi:d}.mp4\' contains excluded tag \'{mtag}\'!',
                             LoggingFlags.LOGGING_EX_EXCLUDED_TAGS)
                elif mtag is None and extag[0] != '-':
                    suc = False
                    Log.trace(f'[{subfolder}] Video \'{prefixp()}{idi:d}.mp4\' misses required tag matching \'{my_extag}\'!',
                              LoggingFlags.LOGGING_EX_MISSING_TAGS)
    return not suc


def get_matching_scenario_subquery_idx(idi: int, tags_raw: List[str], likes: str, scenario: DownloadScenario) -> int:
    for idx, sq in enumerate(scenario.queries):
        if not is_filtered_out_by_extra_tags(idi, tags_raw, sq.extra_tags, sq.subfolder):
            if len(likes) > 0:
                try:
                    if int(likes) < sq.minscore:
                        Log.info(f'[{sq.subfolder}] Video \'{prefixp()}{idi:d}.mp4\''
                                 f' has low score \'{int(likes):d}\' (required {sq.minscore:d})!',
                                 LoggingFlags.LOGGING_EX_LOW_SCORE)
                        continue
                except Exception:
                    pass
            return idx
    return -1


def get_uvp_always_subquery_idx(scenario: DownloadScenario) -> int:
    for idx, sq in enumerate(scenario.queries):
        if sq.uvp == DOWNLOAD_POLICY_ALWAYS:
            return idx
    return -1


async def download_id(idi: int, my_title: str, scenario: Optional[DownloadScenario]) -> None:
    my_subfolder = ''
    my_quality = ExtraConfig.quality
    my_tags = 'no_tags'
    likes = ''
    i_html = await fetch_html(f'{SITE_AJAX_REQUEST_VIDEO % idi}?popup_id={2 + idi % 10:d}', session=download_worker.session)
    if i_html:
        if i_html.find('title', string='404 Not Found'):
            Log.error(f'Got error 404 for {prefixp()}{idi:d}.mp4, skipping...')
            return

        if my_title in [None, '']:
            titleh1 = i_html.find('h1', class_='title_video')
            if titleh1:
                my_title = titleh1.text
            else:
                my_title = ''
        try:
            likespan = i_html.find('span', class_='voters count')
            likes = str(likespan.text[:max(likespan.text.find(' '), 0)])
        except Exception:
            pass
        try:
            my_author = str(i_html.find('div', string='Artist:').parent.find('span').string).lower()
        except Exception:
            Log.warn(f'Warning: cannot extract author for {prefixp()}{idi:d}.mp4.')
            my_author = ''
        try:
            my_categories = [str(a.string).lower() for a in i_html.find('div', string='Categories:').parent.find_all('span')]
        except Exception:
            Log.warn(f'Warning: cannot extract categories for {prefixp()}{idi:d}.mp4.')
            my_categories = []
        try:
            tdiv = i_html.find('div', string='Tags:')
            tags = tdiv.parent.find_all('a', class_='tag_item')
            tags_raw = [str(tag.string).replace(' ', '_').lower() for tag in tags]
            for add_tag in [ca.replace(' ', '_') for ca in my_categories + [my_author] if len(ca) > 0]:
                if add_tag not in tags_raw:
                    tags_raw.append(add_tag)
            if is_filtered_out_by_extra_tags(idi, tags_raw, ExtraConfig.extra_tags, my_subfolder):
                Log.info(f'Info: video {prefixp()}{idi:d}.mp4 is filtered out by outer extra tags, skipping...')
                return
            if len(likes) > 0:
                try:
                    if int(likes) < ExtraConfig.min_score:
                        Log.info(f'Info: video {prefixp()}{idi:d}.mp4'
                                 f' has low score \'{int(likes):d}\' (required {ExtraConfig.min_score:d}), skipping...')
                        return
                except Exception:
                    pass
            if scenario is not None:
                sub_idx = get_matching_scenario_subquery_idx(idi, tags_raw, likes, scenario)
                if sub_idx == -1:
                    Log.info(f'Info: unable to find matching scenario subquery for {prefixp()}{idi:d}.mp4, skipping...')
                    return
                my_subfolder = scenario.queries[sub_idx].subfolder
                my_quality = scenario.queries[sub_idx].quality
            if ExtraConfig.save_tags:
                register_item_tags(idi, ' '.join(sorted(tags_raw)), my_subfolder)
            tags_str = filtered_tags(list(sorted(tags_raw)))
            if tags_str != '':
                my_tags = tags_str
        except Exception:
            if scenario is not None:
                uvp_idx = get_uvp_always_subquery_idx(scenario)
                if uvp_idx == -1:
                    Log.warn(f'Warning: could not extract tags from {prefixp()}{idi:d}.mp4, '
                             f'skipping due to untagged videos download policy (scenario)...')
                    return
                my_subfolder = scenario.queries[uvp_idx].subfolder
                my_quality = scenario.queries[uvp_idx].quality
            elif len(ExtraConfig.extra_tags) > 0 and ExtraConfig.uvp != DOWNLOAD_POLICY_ALWAYS:
                Log.warn(f'Warning: could not extract tags from {prefixp()}{idi:d}.mp4, skipping due to untagged videos download policy...')
                return
            Log.warn(f'Warning: could not extract tags from {prefixp()}{idi:d}.mp4...')

        tries = 0
        while True:
            ddiv = i_html.find('div', string='Download:')
            if ddiv is not None and ddiv.parent is not None:
                break
            reason = 'probably an error'
            del_span = i_html.find('span', class_='message')
            if del_span:
                reason = f'reason: \'{str(del_span.text)}\''
            Log.error(f'Cannot find download section for {prefixp()}{idi:d}.mp4, {reason}, skipping...')
            tries += 1
            if tries >= 5:
                download_worker.failed_items.append(idi)
                return
            elif reason != 'probably an error':
                return
            i_html = await fetch_html(SITE_AJAX_REQUEST_VIDEO % idi, session=download_worker.session)

        links = ddiv.parent.find_all('a', class_='tag_item')
        qualities = []
        for lin in links:
            q = search(r'(\d+p)', str(lin.text))
            if q:
                qualities.append(q.group(1))

        if not (my_quality in qualities):
            q_idx = 0
            Log.warn(f'Warning: cannot find quality \'{my_quality}\' for {prefixp()}{idi:d}.mp4, using \'{qualities[q_idx]}\'')
            my_quality = qualities[q_idx]
            link_idx = q_idx
        else:
            link_idx = qualities.index(my_quality)

        link = links[link_idx].get('href')
    else:
        Log.error(f'Error: unable to retreive html for {prefixp()}{idi:d}.mp4! Aborted!')
        download_worker.failed_items.append(idi)
        return

    my_dest_base = normalize_path(f'{ExtraConfig.dest_base}{my_subfolder}')
    my_score = f'+{likes}' if len(likes) > 0 else 'unk'
    extra_len = 5 + 2 + 2  # 2 underscores + 2 brackets + len('2160p') - max len of all qualities
    fname_part2 = f'{my_quality}_pydw{extract_ext(link)}'
    fname_part1 = (
        f'{prefixp() if has_naming_flag(NamingFlags.NAMING_FLAG_PREFIX) else ""}'
        f'{idi:d}'
        f'{f"_score({my_score})" if has_naming_flag(NamingFlags.NAMING_FLAG_SCORE) else ""}'
        f'{f"_{my_title}" if my_title != "" and has_naming_flag(NamingFlags.NAMING_FLAG_TITLE) else ""}'
    )
    if has_naming_flag(NamingFlags.NAMING_FLAG_TAGS):
        while len(my_tags) > max(0, 240 - (len(my_dest_base) + len(fname_part1) + len(fname_part2) + extra_len)):
            my_tags = my_tags[:max(0, my_tags.rfind(TAGS_CONCAT_CHAR))]
        fname_part1 = f'{fname_part1}{f"_({my_tags})" if len(my_tags) > 0 else ""}'

    if len(my_tags) == 0 and len(fname_part1) > max(0, 240 - (len(my_dest_base) + len(fname_part2) + extra_len)):
        fname_part1 = fname_part1[:max(0, 240 - (len(my_dest_base) + len(fname_part2) + extra_len))]
    filename = f'{fname_part1}_{fname_part2}'

    await download_file(idi, filename, my_dest_base, link, my_subfolder)


async def download_file(idi: int, filename: str, my_dest_base: str, link: str, subfolder='') -> int:
    dest = normalize_filename(filename, my_dest_base)
    sfilename = f'{f"{subfolder}/" if len(subfolder) > 0 else ""}{filename}'
    file_size = 0
    retries = 0
    ret = DownloadResult.DOWNLOAD_SUCCESS

    if not path.isdir(my_dest_base):
        try:
            makedirs(my_dest_base)
        except Exception:
            raise IOError(f'ERROR: Unable to create subfolder \'{my_dest_base}\'!')
    else:
        rv_match = match(re_rvfile, filename)
        rv_quality = rv_match.group(2)
        if file_exists_in_folder(my_dest_base, idi, rv_quality, False):
            Log.info(f'{filename} (or similar) already exists. Skipped.')
            return DownloadResult.DOWNLOAD_FAIL_ALREADY_EXISTS

    # filename_short = 'rv_' + str(idi)
    # Log('Retrieving %s...' % filename_short)
    while (not (path.isfile(dest) and file_size > 0)) and retries < CONNECT_RETRIES_ITEM:
        try:
            if ExtraConfig.dm == DOWNLOAD_MODE_TOUCH:
                Log.info(f'Saving<touch> {0.0:.2f} Mb to {sfilename}')
                with open(dest, 'wb'):
                    pass
                break

            r = None
            # timeout must be relatively long, this is a timeout for actual download, not just connection
            async with await wrap_request(download_worker.session, 'GET', link, timeout=7200, headers={'Referer': link}) as r:
                if r.status == 404:
                    Log.error(f'Got 404 for {prefixp()}{idi:d}.mp4...!')
                    retries = CONNECT_RETRIES_ITEM - 1
                    ret = DownloadResult.DOWNLOAD_FAIL_NOT_FOUND
                if r.content_type and r.content_type.find('text') != -1:
                    Log.error(f'File not found at {link}!')
                    raise FileNotFoundError(link)

                expected_size = r.content_length
                Log.info(f'Saving {(r.content_length / (1024.0 * 1024.0)) if r.content_length else 0.0:.2f} Mb to {sfilename}')

                download_worker.writes_active.append(dest)
                async with async_open(dest, 'wb') as outf:
                    async for chunk in r.content.iter_chunked(2**22):
                        await outf.write(chunk)
                download_worker.writes_active.remove(dest)

                file_size = stat(dest).st_size
                if expected_size and file_size != expected_size:
                    Log.error(f'Error: file size mismatch for {sfilename}: {file_size:d} / {expected_size:d}')
                    raise IOError(link)
                break
        except Exception:
            import sys
            print(sys.exc_info()[0], sys.exc_info()[1])
            if r is None or r.status != 403:
                retries += 1
                Log.error(f'{sfilename}: error #{retries:d}...')
            if r is not None:
                r.close()
            if path.isfile(dest):
                remove(dest)
            # Network error may be thrown before item is added to active download
            if dest in download_worker.writes_active:
                download_worker.writes_active.remove(dest)
            if retries >= CONNECT_RETRIES_ITEM and ret != DownloadResult.DOWNLOAD_FAIL_NOT_FOUND:
                download_worker.failed_items.append(idi)
                break
            await sleep(frand(1.0, 7.0))
            continue

    ret = (ret if ret == DownloadResult.DOWNLOAD_FAIL_NOT_FOUND else
           DownloadResult.DOWNLOAD_SUCCESS if retries < CONNECT_RETRIES_ITEM else
           DownloadResult.DOWNLOAD_FAIL_RETRIES)
    return ret


def at_interrupt() -> None:
    if download_worker is None:
        return

    if len(download_worker.writes_active) > 0:
        Log.debug(f'at_interrupt: cleaning {len(download_worker.writes_active):d} unfinished files...')
        for unfinished in sorted(download_worker.writes_active):
            Log.debug(f'at_interrupt: trying to remove \'{unfinished}\'...')
            if path.isfile(unfinished):
                remove(unfinished)
            else:
                Log.debug(f'at_interrupt: file \'{unfinished}\' not found!')

#
#
#########################################
