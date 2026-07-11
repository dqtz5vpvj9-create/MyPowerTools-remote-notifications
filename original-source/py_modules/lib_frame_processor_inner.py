import json
import traceback
from typing import OrderedDict, Optional
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
import pandas as pd
import numpy as np
import os
import socket
import os
import threading
import signal
from datetime import datetime as datetime_class
import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path

def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
    try:
        sys.path.remove(str(parent))
    except ValueError: # already removed
        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()

from py_modules.AppTestHelper import AppTestHelper, SwipeType
from py_modules.lib_aosp_base import *
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_appium import AppiumWrapper, PreExecuteResult
from test_tools.am_pm_utils import get_foreground_activities, get_last_displayId
from test_tools.ci_base import Ci_Base
def gen_frame_records(filter: str, trace_path: str, output_path: str):
    tp = TraceProcessor(
        trace=trace_path, 
        config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
    )
    track_type = {
        # track_id => track_type
    }

    track_activity = {
        # track_id => activity_index
    }
    
    slice_data = {
        # <ACTIVITY>_<ID> => [ APP_START, APP_END, GPU_START, GPU_END, SF_START, SF_END, Duration ]
    }

    slice_itr = tp.query("select id, name, ts, dur, track_id from slice where dur > 0")
    track_itr = tp.query("select id, name from track")
    
    # iterate track
    for track in track_itr:
        track_name = str(track.name)
        if filter not in track_name:
            continue

        track_id = int(track.id)
    
        if track_name.startswith("APP_"):
            track_type[track_id] = "APP"
            track_activity[track_id] = int(track_name.split()[0].lstrip("APP_"))
        elif track_name.startswith("GPU_"):
            track_type[track_id] = "GPU"
            track_activity[track_id] = int(track_name.split()[0].lstrip("GPU_"))
        elif track_name.startswith("SF_"):
            track_type[track_id] = "SF"
            track_activity[track_id] = int(track_name.split()[0].lstrip("SF_"))

    for slice in slice_itr:
        if slice.track_id not in track_type:
            continue

        slice_id = f"{track_activity[slice.track_id]}_{slice.name}"
        slice_type = track_type[slice.track_id]
            
        if slice_id not in slice_data:
            slice_data[slice_id] = [0, 0, 0, 0, 0, 0, 0]

        try:
            if slice_type == "APP":
                assert(slice_data[slice_id][0] == 0)
                assert(slice_data[slice_id][1] == 0)
                slice_data[slice_id][0] = int(slice.ts)
                slice_data[slice_id][1] = int(slice.ts) + int(slice.dur)
            elif slice_type == "GPU":
                assert(slice_data[slice_id][2] == 0)
                assert(slice_data[slice_id][3] == 0)
                slice_data[slice_id][2] = int(slice.ts)
                slice_data[slice_id][3] = int(slice.ts) + int(slice.dur)
            elif slice_type == "SF":
                assert(slice_data[slice_id][4] == 0)
                assert(slice_data[slice_id][5] == 0)
                slice_data[slice_id][4] = int(slice.ts)
                slice_data[slice_id][5] = int(slice.ts) + int(slice.dur)
        except AssertionError as e:
            print("slice_id: ", slice_id, "slice_type: ", slice_type)
            print(slice_data[slice_id])
            print(slice)
            raise e

    # calculate duration = GPU_END - APP_START
    for slice_id in slice_data:
        # invalid slice
        if slice_data[slice_id][0] == 0 or slice_data[slice_id][3] == 0:
            continue
        slice_data[slice_id][6] = slice_data[slice_id][3] - slice_data[slice_id][0]

    # convert slice_data to dataframe
    df = pd.DataFrame.from_dict(slice_data, 
        orient='index', 
        columns=['APP_START', 'APP_END', 'GPU_START', 'GPU_END', 'SF_START', 'SF_END', 'Duration'])
    
    df.to_csv(output_path)

def gen_fps_summary(trace_path: str, output_path: str):
    tp = TraceProcessor(
        trace=trace_path, 
        config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
    )

    track_itr = tp.query("select id from track where name = 'Display_com.twitte'")
    if len(track_itr) != 1:
        raise Exception("Invalid display track count")
    # get the only item from iterator
    display_track_id = next(track_itr).id

    display_slices = tp \
        .query(f"select id, name, ts, dur from slice where track_id = {display_track_id}") \
        .as_pandas_dataframe()

    # ts is in ns, convert to s and round down
    display_slices['second_belong'] = display_slices['ts'] // 1000000000
    print(display_slices)

    # count fps for each distinct second_belong
    fps_df = display_slices \
        .groupby('second_belong') \
        .size() \
        .reset_index(name='fps')
    
    fps_df.to_csv(output_path, index=False)

def get_track_data(tp: TraceProcessor, sql_query: str, type: str='slice'):
    if type not in ["slice", "counter"]:
        return None

    track_itr = tp.query(sql_query)
    if len(track_itr) != 1:
        return None
    track_id = int(next(track_itr).id)
    return tp.query(f"SELECT * FROM {type} WHERE track_id = {track_id} ORDER BY ts")

def find_biggest_recorddraw_before(df: pd.DataFrame, ts: int):
    # row: [id, ts, dur]
    # find the ts biggest one which row.ts + row.dur < ts
    # return None if not found
    candidate = df[df['ts'] + df['dur'] < ts]
    if len(candidate) == 0:
        return None
    else:
        return candidate.iloc[np.argmax(candidate['ts'])]


def get_next_or_None(itr: TraceProcessor.QueryResultIterator):
    try:
        return next(itr)
    except StopIteration:
        return None

def process_frame_trace(trace_path: str, target_process: str, main_proc_pid: Optional[int] = None, target_thread: Optional[str] = None, result_dict = None, debug: bool = False, max_frames: Optional[int] = None) -> pd.DataFrame:
    tp = TraceProcessor(
        trace=trace_path, 
        config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
    )

    try:
        frame_timeline = tp.query("""select * 
                    from (actual_frame_timeline_slice)""")
        frame_timeline = frame_timeline.as_pandas_dataframe()
    except Exception as e:
        frame_timeline = None
    
    try:
        expected_frame_timeline = tp.query("""select * 
                    from (expected_frame_timeline_slice)""")
        expected_frame_timeline = expected_frame_timeline.as_pandas_dataframe()
    except Exception as e:
        expected_frame_timeline = None

    vsync_app = get_track_data(tp, "SELECT id FROM track WHERE name = 'VSYNC-app'", type='counter')
    vsync_app_count = 0

    vsync_sf_itr  = get_track_data(tp, "SELECT id FROM track WHERE name = 'VSYNC-sf'", type='counter')
    if vsync_sf_itr is None:
        return pd.DataFrame([], columns=[
            'ts', 'vsync_app_ts', 'choreographer_start', 'choreographer_end', 'recorddraw_start', 'recorddraw_end', 
            'drawframe_start', 'drawframe_end', 'gpufence_start', 'gpufence_end', 'gpucompletion_start', 'gpucompletion_end', 'vsync_sf_ts', 
            'deadline', 'is_drop', 'rendering_time'
        ])
    vsync_sf = get_next_or_None(vsync_sf_itr) # get the first one
    vsync_sf_count = 0

    # Get target process upid and expected frame names
    target_upid = get_target_upid(tp, target_process, main_proc_pid)
    if target_upid is None:
        print(f"Warning: Could not find upid for target process {target_process}")
        expected_frame_names = set()
    else:
        expected_frame_names = get_expected_frame_names(tp, target_upid)
        if debug:
            print(f"Found target process upid: {target_upid}")
            print(f"Expected frame names count: {len(expected_frame_names)}")

    tid_query = f"AND thread_name = '{target_thread}'" \
                if target_thread is not None \
                else "AND pid = tid"
    pid_query = f"AND process_name = '{target_process}'" \
                if main_proc_pid is None \
                else f"AND pid = {main_proc_pid}"

    
    # app phase
    choreographer_itr = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur, name, pid, tid, process_name FROM experimental_slice_with_thread_and_process_info \
            WHERE name LIKE 'Choreographer#doFrame%' \
            {pid_query} \
            {tid_query} \
        ORDER BY ts")
    choreographer = get_next_or_None(choreographer_itr) # get the first one
    choreographer_count = 0

    # sub slice of choreographer, draw frame must behind record draw
    recorddraw_df = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur, pid, tid FROM experimental_slice_with_thread_and_process_info \
            WHERE name = 'Record View#draw()' \
            {pid_query} \
            {tid_query} \
        ORDER BY ts").as_pandas_dataframe()
    
    # gpu phase
    drawframe_itr = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur, name FROM experimental_slice_with_thread_and_process_info \
            WHERE name LIKE 'DrawFrame%' \
            {pid_query} \
            AND thread_name = 'RenderThread' \
        ORDER BY ts")
    drawframe = get_next_or_None(drawframe_itr) # get the first one
    drawframe_count = 0

    # sub slice of drawframe, to compare with deadline
    gpufence_itr = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur FROM experimental_slice_with_thread_and_process_info \
            WHERE name LIKE 'Trace GPU Completion fence %' \
            {pid_query} \
            AND thread_name = 'RenderThread' \
        ORDER BY ts")
    gpufence = get_next_or_None(gpufence_itr) # get the first one
    gpufence_count = 0
    
    # Add support for "GPU completion fence X has signaled" type slices in RenderThread
    gpu_signaled_itr = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur FROM experimental_slice_with_thread_and_process_info \
            WHERE name LIKE 'GPU completion fence % has signaled' \
            {pid_query} \
            AND thread_name = 'RenderThread' \
        ORDER BY ts")
    gpu_signaled = get_next_or_None(gpu_signaled_itr)
    gpu_signaled_count = 0
    
    gpucompletion_itr = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur FROM experimental_slice_with_thread_and_process_info \
            WHERE name LIKE 'waiting for GPU completion %' \
            {pid_query} \
            AND thread_name = 'GPU completion' \
        ORDER BY ts")
    gpucompletion = get_next_or_None(gpucompletion_itr)
    gpucompletion_count = 0

    frame_data = []
    found_frame_indexes = set()
    processed_frames = 0

    for vsync_app_signal in vsync_app:
        vsync_app_count += 1
        vsync_app_ts = vsync_app_signal.ts
        
        if debug:
            print(f"\n--- Processing VSYNC-app #{vsync_app_count}, ts={vsync_app_ts} ---")
        
        # find the first choreographer frame after this vsync_app
        while choreographer is not None and choreographer.ts < vsync_app_ts:
            choreographer = get_next_or_None(choreographer_itr)
            choreographer_count += 1
        if choreographer is None:
            if debug:
                print("No more choreographer frames, breaking")
            break
    
        # Extract frame number from choreographer name to validate against expected frame timeline
        choreographer_frame_name = None
        try:
            # Access name using the correct attribute - now that we include name in the SELECT
            choreographer_name = str(choreographer.name) if hasattr(choreographer, 'name') else ""
            if "Choreographer#doFrame" in choreographer_name:
                parts = choreographer_name.split()
                if len(parts) >= 2:
                    choreographer_frame_name = parts[-1]  # Get the last part which should be the frame number
                    if debug:
                        print(f"Extracted frame name: {choreographer_frame_name} from: {choreographer_name}")
        except Exception as e:
            if debug:
                print(f"Error extracting frame name: {e}")
            choreographer_frame_name = None
        
        # Validate choreographer frame against expected frame timeline
        if choreographer_frame_name is not None and expected_frame_names:
            if choreographer_frame_name not in expected_frame_names:
                if debug:
                    print(f"Skipping: Choreographer frame {choreographer_frame_name} not found in expected frame timeline")
                continue
        elif expected_frame_names:  # If we have expected frames but couldn't extract frame name
            if debug:
                print(f"Skipping: Could not extract frame name from choreographer: {getattr(choreographer, 'name', 'Unknown')}")
            continue

        # Check expected frame timeline timing if available
        if choreographer_frame_name and expected_frame_timeline is not None:
            expected_frame_info = expected_frame_timeline[expected_frame_timeline['name'] == choreographer_frame_name]
            if not expected_frame_info.empty:
                expected_ts = expected_frame_info['ts'].iloc[0]
                # Validate the timing sequence: expected_frame_ts -> vsync_app_ts -> choreographer_ts
                if expected_ts > vsync_app_ts or expected_ts > choreographer.ts:
                    if debug:
                        print(f"Skipping: Invalid timing sequence for frame {choreographer_frame_name}: expected={expected_ts}, vsync_app={vsync_app_ts}, choreographer={choreographer.ts}")
                    continue

        if debug:
            print(f"Found valid choreographer: id={choreographer.id}, ts={choreographer.ts}, dur={choreographer.dur}, frame={choreographer_frame_name}")

        # find its record draw
        recorddraw = find_biggest_recorddraw_before(recorddraw_df, choreographer.ts + choreographer.dur)
        if recorddraw is None or \
            recorddraw.ts + recorddraw.dur >= choreographer.ts + choreographer.dur or \
            recorddraw.ts < choreographer.ts:
            # this choreographer frame doesn't have a corresponding record draw
            if debug:
                if recorddraw is None:
                    print(f"Skipping: No corresponding Record View#draw() found")
                else:
                    print(f"Skipping: Record draw timing issue - recorddraw_ts={recorddraw.ts}, recorddraw_end={recorddraw.ts + recorddraw.dur}, choreographer_start={choreographer.ts}, choreographer_end={choreographer.ts + choreographer.dur}")
            continue
        
        if debug:
            print(f"Found recorddraw: ts={recorddraw.ts}, dur={recorddraw.dur}")
        
        # DrawFrame must later than `Record View#draw()`'s completion
        earliest_drawframe = recorddraw.ts + recorddraw.dur
        # print(f"VSYNC-app: {vsync_app_ts}, Choreographer: {choreographer.ts}, gap = {choreographer.ts - vsync_app_ts}")
        # find corresponding drawframe & vsync_sf
        while drawframe is not None and drawframe.ts < earliest_drawframe:
            drawframe = get_next_or_None(drawframe_itr)
            drawframe_count += 1
        
        if drawframe is None:
            if debug:
                print("No more drawframes, breaking")
            break

        if choreographer.ts + choreographer.dur < drawframe.ts:
            # this choreographer frame doesn't have a corresponding drawframe
            if debug:
                print(f"Skipping: No corresponding drawframe - choreographer_end={choreographer.ts + choreographer.dur} < drawframe_start={drawframe.ts}")
            continue

        if debug:
            print(f"Found drawframe: id={drawframe.id}, ts={drawframe.ts}, dur={drawframe.dur}, name={drawframe.name}")

        # find corresponding gpufence or gpu_signaled
        # First try to find traditional "Trace GPU Completion fence" slice
        while gpufence is not None and gpufence.ts < drawframe.ts:
            gpufence = get_next_or_None(gpufence_itr)
            gpufence_count += 1
        
        # Also advance gpu_signaled iterator to match timeframe
        while gpu_signaled is not None and gpu_signaled.ts < drawframe.ts:
            gpu_signaled = get_next_or_None(gpu_signaled_itr)
            gpu_signaled_count += 1
        
        # Check which type of GPU completion slice we should use
        gpu_fence_to_use = None
        gpu_fence_type = None
        
        # Check if we have a traditional GPU fence within the drawframe timespan
        if (gpufence is not None and 
            gpufence.ts >= drawframe.ts and 
            gpufence.ts + gpufence.dur < drawframe.ts + drawframe.dur):
            gpu_fence_to_use = gpufence
            gpu_fence_type = "trace_gpu_fence"
            # find corresponding gpucompletion
            while gpucompletion is not None and gpucompletion.ts < gpu_fence_to_use.ts + gpu_fence_to_use.dur:
                gpucompletion = get_next_or_None(gpucompletion_itr)
                gpucompletion_count += 1
        
        # Check if we have a "GPU completion fence X has signaled" slice within the drawframe timespan
        if (gpu_signaled is not None and 
            gpu_signaled.ts >= drawframe.ts and 
            gpu_signaled.ts + gpu_signaled.dur < drawframe.ts + drawframe.dur):
            # If we don't have a traditional fence, or if the signaled one comes first, use it
            if gpu_fence_to_use is None or gpu_signaled.ts < gpu_fence_to_use.ts:
                gpu_fence_to_use = gpu_signaled
                gpu_fence_type = "inline_gpu_fence"
        
        if gpu_fence_to_use is None:
            # this drawframe doesn't have a corresponding gpu fence of either type
            if debug:
                print(f"Skipping: No corresponding GPU fence found (neither 'Trace GPU Completion fence' nor 'GPU completion fence has signaled')")
            continue
        
        if debug:
            print(f"Found {gpu_fence_type}: ts={gpu_fence_to_use.ts}, dur={gpu_fence_to_use.dur}")
        
        gpu_fence_end_ts = gpu_fence_to_use.ts + gpu_fence_to_use.dur
        gpucompletion_start = gpucompletion.ts if gpucompletion is not None else gpu_fence_end_ts
        gpucompletion_end = gpucompletion.ts + gpucompletion.dur if gpucompletion is not None else gpu_fence_end_ts

        while vsync_sf is not None and vsync_sf.ts < vsync_app_ts:
            vsync_sf = get_next_or_None(vsync_sf_itr)
            vsync_sf_count += 1

        if vsync_sf is None:
            if debug:
                print("No vsync_sf found, skipping")
            continue

        deadline = None
        is_drop = None
        # 将当前帧的所有信息记录下来
        drawframe_name  = drawframe.name
        try:
            frame_idx = int(drawframe_name.split(' ')[-1])
        except ValueError:
            frame_idx = None
        if frame_idx is not None and frame_timeline is not None:
            expected_frame_info = expected_frame_timeline[expected_frame_timeline['name'] == str(frame_idx)]
            expected_frame_info = expected_frame_info.sort_values(by='dur', ascending=False).iloc[0:1]
            frame_info = frame_timeline[frame_timeline['name'] == str(frame_idx)]
            frame_info = frame_info.sort_values(by='dur', ascending=False).iloc[0:1]
            if frame_info is not None:
                expected_frame_info_ts = expected_frame_info['ts'].values[0] if not expected_frame_info.empty else None
                expected_frame_info_dur = expected_frame_info['dur'].values[0] if not expected_frame_info.empty else None
                expected_frame_end_ts = expected_frame_info_ts + expected_frame_info_dur if expected_frame_info_ts is not None and expected_frame_info_dur is not None else None
                deadline = expected_frame_end_ts
                is_drop = (gpucompletion_end > deadline) if deadline is not None else None
                frame_info_ts = frame_info['ts'].values[0] if not frame_info.empty else None
                frame_info_dur =  frame_info['dur'].values[0] if not frame_info.empty else None
                total_frame_duration = frame_info_ts + frame_info_dur - expected_ts if expected_ts is not None and frame_info_ts is not None and frame_info_dur is not None else None
                frame_info_jank_type = frame_info['jank_type'].values[0] if not frame_info.empty else None
                frame_info_jank_tag = frame_info['jank_tag'].values[0] if not frame_info.empty else None
                # Present type
                present_type = frame_info['present_type'].values[0] if not frame_info.empty else None
                # On time finish
                on_time_finish = frame_info['on_time_finish'].values[0] if not frame_info.empty else None
                gpu_composition = frame_info['gpu_composition'].values[0] if not frame_info.empty else None
                prediction_type = frame_info['prediction_type'].values[0] if not frame_info.empty else None
                layer_name = frame_info['layer_name'].values[0] if not frame_info.empty else None
                if frame_info_jank_tag is not None and frame_info_jank_tag != 'No Jank' \
                    and frame_info_jank_tag != 'Buffer Stuffing' and present_type != 'Early Present':
                    severe_jank = True
                else:
                    severe_jank = False
        else:
            expected_frame_info_ts = None
            expected_frame_info_dur = None
            expected_frame_end_ts = None
            frame_info_ts = None
            frame_info_dur = None
            total_frame_duration = None
            frame_info_jank_type = None
            frame_info_jank_tag = None
            present_type = None
            on_time_finish = None
            gpu_composition = None
            prediction_type = None
            layer_name = None
            severe_jank = False

        if debug:
            print(f"Frame analysis: deadline={deadline}, gpu_fence_end={gpu_fence_to_use.ts + gpu_fence_to_use.dur}, is_severe_jank={severe_jank}, rendering_time={total_frame_duration}ns")
        if frame_idx is not None and frame_idx in found_frame_indexes:
            if debug:
                print(f"Skipping frame #{frame_idx} as it has already been processed")
            continue
        if frame_idx is not None:
            found_frame_indexes.add(frame_idx)
        frame_data.append({
            'ts': choreographer.ts,
            'vsync_app_ts': vsync_app_ts,
            'choreographer_start': choreographer.ts,
            'choreographer_end': choreographer.ts + choreographer.dur,
            'recorddraw_start': recorddraw.ts,
            'recorddraw_end': recorddraw.ts + recorddraw.dur,
            'drawframe_start': drawframe.ts,
            'drawframe_end': drawframe.ts + drawframe.dur,
            'gpufence_start': gpu_fence_to_use.ts,
            'gpufence_end': gpu_fence_end_ts,
            'gpucompletion_start': gpucompletion_start,
            'gpucompletion_end': gpucompletion_end,
            'vsync_sf_ts': vsync_sf.ts,
            'deadline': deadline,
            'is_drop': is_drop,
            'rendering_time': total_frame_duration,
            'frame_idx': frame_idx,
            'frame_info_ts': frame_info_ts,
            'frame_info_dur': frame_info_dur,
            'exp_ts': expected_frame_info_ts,
            'exp_dur': expected_frame_info_dur,
            'exp_end_ts': expected_frame_end_ts,
            'total_frame_duration': total_frame_duration,
            'frame_info_jank_type': frame_info_jank_type,
            'frame_info_jank_tag': frame_info_jank_tag,
            'present_type': present_type,
            'on_time_finish': on_time_finish,
            'gpu_composition': gpu_composition,
            'prediction_type': prediction_type,
            'layer_name': layer_name,
            'frame_info_dur_ms': frame_info_dur // 1E4 / 100 if frame_info_dur is not None else None,
            'overdue_ms': expected_frame_end_ts - (frame_info_ts + frame_info_dur) // 1E4 / 100 if expected_frame_end_ts is not None and frame_info_ts is not None and frame_info_dur is not None else None,
            'overdue_weight': (frame_info_ts + frame_info_dur - expected_frame_info_ts) / (expected_frame_info_dur) if expected_frame_info_ts is not None and expected_frame_info_dur is not None and frame_info_ts is not None and frame_info_dur is not None else None,
            'severe_jank': severe_jank,
        })
        
        processed_frames += 1
        if debug:
            print(f"Successfully processed frame #{processed_frames}")
        
        # Check if we've reached the maximum number of frames
        if max_frames is not None and processed_frames >= max_frames:
            if debug:
                print(f"Reached maximum frames limit ({max_frames}), stopping")
            break

    df = pd.DataFrame(frame_data, columns=[
        'ts', 'vsync_app_ts', 'choreographer_start', 'choreographer_end', 
        'recorddraw_start', 'recorddraw_end', 
        'drawframe_start', 'drawframe_end', 
        'gpufence_start', 'gpufence_end', 
        'gpucompletion_start', 'gpucompletion_end',
        'vsync_sf_ts', 'deadline', 'is_drop', 'rendering_time',
        'frame_idx', 'frame_info_ts', 'frame_info_dur',
        'exp_ts', 'exp_dur', 'exp_end_ts', 'total_frame_duration',
        'frame_info_jank_type', 'frame_info_jank_tag',
        'present_type', 'on_time_finish', 'gpu_composition',
        'prediction_type', 'layer_name', 
        'frame_info_dur_ms', 'overdue_ms', 'overdue_weight',
        'severe_jank'
    ])

    if len(df) != 0:
        drop_count = len(df[df['severe_jank'] == True])
        print(f"{drop_count} out of {len(df)} frames are severe_jank. severe_jank_rate = {drop_count / len(df)}")
    else:
        drop_count = 0

    avg_render = df['rendering_time'].mean() // 1E6
    print(f"vsync_app_count: {vsync_app_count}")
    print(f"vsync_sf_count: {vsync_sf_count}")
    print(f"choreographer_count: {choreographer_count}")
    print(f"drawframe_count: {drawframe_count}")
    print(f"gpufence_count: {gpufence_count}")
    print(f"gpu_signaled_count: {gpu_signaled_count}")
    print(f"Average rendering time: {avg_render}")
    if result_dict is not None:
        result_dict['nr_frames'] = len(df)
        result_dict['nr_jank_frames'] = drop_count
        result_dict['jank_rate'] = drop_count / len(df) if len(df) != 0 else 0
        result_dict['vsync_app_count'] = vsync_app_count
        result_dict['vsync_sf_count'] = vsync_sf_count
        result_dict['choreographer_count'] = choreographer_count
        result_dict['drawframe_count'] = drawframe_count
        result_dict['gpufence_count'] = gpufence_count
        result_dict['gpu_signaled_count'] = gpu_signaled_count
        result_dict['gpucompletion_count'] = gpucompletion_count
        result_dict['average_rendering_time'] = avg_render
    else:
        logger = setup_logging()
        logger.warning("result_dict is None, no result will be saved")

    return df

def calculate_frame_present(trace_path: str, output_path: str):
    tp = TraceProcessor(
        trace=trace_path, 
        config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
    )

    surfaceflinger_df = tp.query(
        f"SELECT IMPORT('experimental.slices'); \
        SELECT id, ts, dur FROM experimental_slice_with_thread_and_process_info \
            WHERE name = 'finishFrame' \
            AND process_name = '/system/bin/surfaceflinger' \
            AND thread_name = 'surfaceflinger' \
        ORDER BY ts").as_pandas_dataframe()
    surfaceflinger_df.to_csv(output_path, index=False)

def get_target_upid(tp: TraceProcessor, target_process: str, main_proc_pid: Optional[int] = None) -> Optional[int]:
    """
    根据进程名或PID获取目标进程的upid
    """
    try:
        if main_proc_pid is not None:
            # 根据PID查找upid
            result = tp.query(f"SELECT upid FROM process WHERE pid = {main_proc_pid}")
        else:
            # 根据进程名查找upid
            result = tp.query(f"SELECT upid FROM process WHERE name = '{target_process}'")
        
        result_list = list(result)
        if len(result_list) > 0:
            return int(result_list[0].upid)
        else:
            return None
    except Exception as e:
        return None

def get_expected_frame_names(tp: TraceProcessor, target_upid: int) -> set:
    """
    根据upid从expected_frame_timeline_slice中获取所有唯一的frame name
    """
    try:
        result = tp.query(f"""
            SELECT DISTINCT name 
            FROM expected_frame_timeline_slice 
            WHERE upid = {target_upid}
        """)
        return {str(row.name) for row in result}
    except Exception as e:
        return set()