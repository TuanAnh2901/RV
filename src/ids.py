# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys
from asyncio import run as run_async, sleep
from typing import Sequence, List

from cmdargs import prepare_arglist_ids
from defs import HelpPrintExitException
from config import Config
from util import at_startup
from logger import Log
from download import download, at_interrupt
from path_util import prefilter_existing_items
from tagger import try_parse_id_or_group
from validators import find_and_resolve_config_conflicts
from vinfo import VideoInfo

__all__ = ('main_sync',)


async def main(args: Sequence[str]) -> None:
    try:
        arglist = prepare_arglist_ids(args)
    except HelpPrintExitException:
        return
    except Exception:
        import traceback
        traceback.print_exc()
        Log.fatal('\nUnable to parse cmdline. Exiting.')
        return

    try:
        Config.read(arglist, False)

        id_sequence = try_parse_id_or_group(Config.extra_tags) if Config.use_id_sequence else list()  # type: List[int]
        if Config.use_id_sequence is True and len(id_sequence) == 0:
            Log.fatal(f'\nInvalid ID \'or\' group \'{Config.extra_tags[0] if len(Config.extra_tags) > 0 else ""}\'!')
            raise ValueError

        if find_and_resolve_config_conflicts() is True:
            await sleep(3.0)
    except Exception:
        import traceback
        traceback.print_exc()
        Log.fatal('\nError reading parsed arglist!')
        return

    if len(id_sequence) == 0:
        id_sequence = list(range(Config.start_id, Config.end_id + 1))
    else:
        Config.extra_tags.clear()

    v_entries = [VideoInfo(idi) for idi in id_sequence]
    orig_count = len(v_entries)

    if len(v_entries) > 0:
        prefilter_existing_items(v_entries)

    removed_count = orig_count - len(v_entries)

    if len(v_entries) == 0:
        if 0 < orig_count == removed_count:
            Log.fatal(f'\nAll {orig_count:d} videos already exist. Aborted.')
        else:
            Log.fatal('\nNo videos found. Aborted.')
        return

    minid, maxid = min(v_entries, key=lambda x: x.my_id).my_id, max(v_entries, key=lambda x: x.my_id).my_id
    Log.info(f'\nOk! {len(v_entries):d} ids (+{removed_count:d} filtered out), bound {minid:d} to {maxid:d}. Working...\n')

    await download(v_entries, True, removed_count)


async def run_main(args: Sequence[str]) -> None:
    await main(args)
    await sleep(0.5)


def main_sync(args: Sequence[str]) -> None:
    assert sys.version_info >= (3, 7), 'Minimum python version required is 3.7!'

    try:
        run_async(run_main(args))
    except (KeyboardInterrupt, SystemExit, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            Log.warn('Warning: catched KeyboardInterrupt/SystemExit...')
        at_interrupt()


if __name__ == '__main__':
    at_startup()
    main_sync(sys.argv[1:])
    exit(0)

#
#
#########################################
