from pathlib import Path
from collections import defaultdict
from typing import Dict, Tuple, Optional, Callable, List
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig
from threading import Lock
import sys
import importlib
import os
import time
import re
import pandas as pd
import copy
import logging
import threading
import concurrent.futures
import time
import functools
import pandas as pd
import threading
import concurrent.futures
import logging
import sys
import os
import importlib
import re
import copy
import glob
import importlib
import math
import sys
import pandas as pd
import re
import os
import concurrent.futures
import threading
import time
TARGET_WORKERS = 64
tp_pool_size = min(16, (os.cpu_count() or 1) + 4)
max_workers = min(TARGET_WORKERS, (os.cpu_count() or 1) + 4)  # 限制最大线程数



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

import copy

from py_modules.logging_lib import setup_logging
from py_modules.lib_frame_processor_inner import process_frame_trace
from py_modules.lib_sched_analyzer import MultiWindowSchedSliceAnalyzer, query_sched_slices
def describe_frame_stages(stage_durations: dict, frame_latency: float, expected_dur: float, frame_idx: int) -> str:
    """
    Describe frame stages breakdown with durations and percentages.
    
    Args:
    stage_durations: Dictionary containing stage duration values
    frame_info_durframe_latency: Total frame duration from exp_ts to gpucompletion_end
    
    Returns:
    String description of frame stages breakdown
    """
    
    stages = [
    ('UI Start Latency', stage_durations.get('ui_stage_start_latency', 0)),
    ('UI Stage', stage_durations.get('total_ui_duration', 0)),
    ('Render Wake-up Latency', stage_durations.get('render_wake_up_latency', 0)),
    ('Draw Stage', stage_durations.get('total_draw_duration', 0)),
    ('GPU Stage', stage_durations.get('total_gpu_stage_duration', 0))
    ]
    
    descriptions = []
    for stage_name, duration in stages:
        if duration > 0:
            percentage = (duration / frame_latency) * 100
            descriptions.append(f"{stage_name}: {duration/1e6:.2f}ms ({percentage:.1f}%)")
    total = f"Total Frame Duration: {frame_latency/1e6:.2f}ms, expected: {expected_dur/1e6:.2f}ms; "
    format_stages = ", ".join([f"{name} {duration/1e6:.2f}ms" for name, duration in stages])
    # logger.info(f"Frame {frame_idx} stages: {format_stages}, Frame latency: {frame_latency/1e6:.2f}ms, Expected duration: {expected_dur/1e6:.2f}ms")
    return total + "; ".join(descriptions) if descriptions else "No valid stage data"

def analyze_single_jank_frame(
    tp: TraceProcessor,
    tp_lock: Lock,
    frame_row: pd.Series,
    main_thread_utid: int,
    render_thread_utid: int,
    logger
) -> dict:
    # 创建带锁的查询函数
    def locked_query_and_convert(sql: str, location: str = "unknown"):
        with tp_lock:
            return tp.query(sql).as_pandas_dataframe()
    
    result_dict = {}
    
    exp_ts = frame_row['exp_ts']
    exp_dur = frame_row['exp_dur']
    choreographer_start = frame_row['choreographer_start']
    choreographer_end = frame_row['choreographer_end']
    recorddraw_end = frame_row['recorddraw_end']
    choreographer_dur = choreographer_end - choreographer_start
    drawframe_start = frame_row['drawframe_start']
    drawframe_end = frame_row['drawframe_end']
    gpufence_end = frame_row['gpufence_end']
    gpucompletion_dur = frame_row['gpucompletion_end'] - frame_row['gpucompletion_start']
    result_dict['gpucompletion_dur'] = gpucompletion_dur
    frame_idx = frame_row['frame_idx'] 

    total_input_duration = 0
    # --- 2. 检查 Input 是否耗时过长 (原因类别 1) ---
    try:
        # 需要 main_thread_utid
        if main_thread_utid is None:
             raise ValueError("Main thread UTID is required for Input analysis")

        input_slices_query = f"""
        SELECT name, ts, dur
        FROM slice
        WHERE track_id = (SELECT id FROM thread_track WHERE utid = {main_thread_utid})
          AND ts >= {choreographer_start} AND ts < {recorddraw_end}
          AND (name = 'input' OR name LIKE 'deliverInputEvent%')
        """
        input_slices = locked_query_and_convert(input_slices_query, "input_slices_analysis")
        if len(input_slices) == 0:
            total_input_duration = 0
        else:
            sorted_input_slices = input_slices.sort_values(by='dur', ascending=False)
            total_input_duration = sorted_input_slices['dur'][0]
    except Exception as e:
        logger.warning(f"[Analyze Frame {frame_idx}] Error querying input slices: {e}")
    result_dict['total_input_duration'] = total_input_duration
    
    # --- 3. 检查主线程其他工作或状态 (原因类别 2) ---
    step_start = time.time()
    try:
        # 需要 main_thread_utid
        if main_thread_utid is None:
             raise ValueError("Main thread UTID is required for Main Thread analysis")

        # 检查方法 1: 查看 Choreographer#doFrame 内部特定非 Input slice 是否过长
        main_thread_slices_query = f"""
        SELECT name, ts, dur
        FROM slice
        WHERE track_id = (SELECT id FROM thread_track WHERE utid = {main_thread_utid})
          AND ts >= {choreographer_start} AND ts < {choreographer_end}
          AND name IN ('animation')
        ORDER BY dur DESC
        """
        main_thread_slices = locked_query_and_convert(main_thread_slices_query, "main_thread_slices_analysis")
        animation_total_dur = main_thread_slices[main_thread_slices['name'] == 'animation'].sort_values(by='dur', ascending=False)['dur'][0] if not main_thread_slices.empty else 0
        result_dict['animation_total_dur'] = animation_total_dur
        vsync_app_ts = frame_row['vsync_app_ts']
        
        # 检查方法 2: 查看 Choreographer#doFrame 整个 slice 是否就过长
        def get_thread_states_for_period(tp: TraceProcessor, utid: int, waker_start: int, period_end: int) -> pd.DataFrame:
            """
            Get thread states for a specified time period, including preceding states.
            
            Args:
            tp: TraceProcessor instance
            utid: UTID of the thread
            period_start: Start timestamp of the period
            period_end: End timestamp of the period
            
            Returns:
            DataFrame containing thread states
            """
            thread_state_query = f"""
            SELECT ts, dur, cpu, state, io_wait, blocked_function, waker_utid
            FROM thread_state
            WHERE utid = {utid}
            AND ((ts < {period_end} AND ts + dur > {waker_start}) OR
                (ts + dur > {waker_start} AND ts < {waker_start}))
            ORDER BY ts
            """
            thread_states = locked_query_and_convert(thread_state_query, "thread_states_analysis")
            # period_states = [copy.deepcopy(thread_states)]
            # pre_period_states = []
            # while thread_states is not None and not thread_states.empty:
            #     first_state = thread_states.iloc[0]
            #     thread_state_query = f"""
            #     SELECT ts, dur, cpu, state, io_wait
            #     FROM thread_state
            #     WHERE utid = {utid}
            #         AND (ts + dur == {first_state['ts']} AND state != 'S' and state != 'Sleeping')
            #     ORDER BY ts
            #     """
            #     thread_states = tp.query(thread_state_query).as_pandas_dataframe()
            #     # fill nan as "NULL"
            #     pre_period_states.append(thread_states)
            # return pd.concat(pre_period_states + period_states, ignore_index=True)
            return thread_states

        choreographer_states = get_thread_states_for_period(tp, main_thread_utid, exp_ts, recorddraw_end)
    

        # | end_state | Translation           |
        # | :-------- | :-------------------- |
        # | R         | Runnable              |
        # | R+        | Runnable (Preempted)  |
        # | S         | Sleeping              |
        # | D         | Uninterruptible Sleep |
        # | T         | Stopped               |
        # | t         | Traced                |
        # | X         | Exit (Dead)           |
        # | Z         | Exit (Zombie)         |
        # | x         | Task Dead             |
        # | I         | Idle                  |
        # | K         | Wake Kill             |
        # | W         | Waking                |
        # | P         | Parked                |
        # | N         | No Load               |


        def analyze_thread_states(
            thread_states: pd.DataFrame,
            analysis_start: int,
            analysis_end: int,
            running_only = False
        ) -> dict:
            """
            Analyze thread states within a specified time range.
            
            Args:
                thread_states: DataFrame containing thread state information with columns:
                              'ts', 'dur', 'cpu', 'state', 'io_wait', 'blocked_function'
                analysis_start: Start timestamp for analysis
                analysis_end: End timestamp for analysis
                
            Returns:
                Dictionary containing analysis results:
                - cpu_running_time: dict mapping cpu -> time spent running on that cpu
                - total_runnable_time: total time in runnable state
                - total_blocked_time: total time in blocked state
                - io_wait_duration: total io wait duration
                - wake_up_latency: wake up latency before analysis period
                - blocked_function_time_dict: dict mapping blocked function -> time
                - sleep_waker_info: dict mapping waker thread name -> sleep time
            """
            cpu_running_time = defaultdict(int)  # cpu -> time
            total_runnable_time = 0
            total_blocked_time = 0
            total_sleeping_time = 0
            io_wait_duration = 0
            wake_up_latency = 0
            blocked_function_time_dict = defaultdict(int)  # blocked_function -> time
            sleep_waker_info = defaultdict(int)  # waker_thread_name -> sleep_time
            runnable_state_cpu_occupiers = []
            sched_slices = query_sched_slices(locked_query_and_convert, analysis_start, analysis_end)

            # 第一遍：收集所有需要的waker_utids
            waker_utids_to_query = set()
            sleep_states_with_wakers = []  # (overlap_dur, waker_utid)
            
            for i, state_row in thread_states.iterrows():
                overlap_start = max(state_row['ts'], analysis_start)
                overlap_end = min(state_row['ts'] + state_row['dur'], analysis_end)
                overlap_dur = max(0, overlap_end - overlap_start)

                if state_row['state'] == 'Running':
                    cpu_running_time[state_row['cpu']] += overlap_dur
                elif running_only:
                    continue
                elif state_row['state'] == 'R' or state_row['state'] == 'R+': 
                    total_runnable_time += overlap_dur
                    runnable_state_cpu_occupiers.append((sched_slices, overlap_start, overlap_end))
                elif state_row['state'] == 'D':
                    total_blocked_time += overlap_dur
                    blocked_function = state_row['blocked_function'] if 'blocked_function' in state_row else None
                    if blocked_function is not None and isinstance(blocked_function, str) and len(blocked_function) > 0:
                        blocked_function_time_dict[blocked_function] += overlap_dur
                elif state_row['state'] == 'S':
                    total_sleeping_time += overlap_dur
                    # 收集waker_utid，稍后批量查询
                    try:
                        current_end_ts = state_row['ts'] + state_row['dur']
                        next_states = thread_states[thread_states['ts'] >= current_end_ts]
                        
                        if not next_states.empty:
                            next_state = next_states.iloc[0]
                            if 'waker_utid' in next_state and not pd.isna(next_state['waker_utid']):
                                waker_utid = int(next_state['waker_utid'])
                                waker_utids_to_query.add(waker_utid)
                                sleep_states_with_wakers.append((overlap_dur, waker_utid))
                    except Exception as e:
                        logger.exception(f"[Analyze Frame {frame_idx}] Error processing sleep waker info: {e}")
                        
                if state_row['io_wait'] is not None:
                    io_wait_duration += state_row['io_wait']
            
            # 第二遍：批量查询所有waker_utid的线程信息
            waker_info_cache = {}  # utid -> waker_name
            if waker_utids_to_query:
                try:
                    utid_list = ','.join(map(str, waker_utids_to_query))
                    batch_waker_query = f"""
                    SELECT t.utid, t.name as thread_name, t.upid, p.name as process_name
                    FROM thread t
                    LEFT JOIN process p ON t.upid = p.upid
                    WHERE t.utid IN ({utid_list})
                    """
                    waker_batch_result = locked_query_and_convert(batch_waker_query, "waker_batch_query")
                    
                    for _, row in waker_batch_result.iterrows():
                        utid = row['utid']
                        waker_name = f"{row['process_name']}/{row['thread_name']}"
                        waker_info_cache[utid] = waker_name
                        
                except Exception as e:
                    logger.exception(f"[Analyze Frame {frame_idx}] Error in batch waker query: {e}")
            
            # 第三遍：使用缓存的waker信息更新sleep_waker_info
            for overlap_dur, waker_utid in sleep_states_with_wakers:
                if waker_utid in waker_info_cache:
                    waker_name = waker_info_cache[waker_utid]
                    sleep_waker_info[waker_name] += overlap_dur
                else:
                    sleep_waker_info[f"Unknown Thread (utid:{waker_utid})"] += overlap_dur
            
            # Calculate wake up latency from states before analysis period
            wake_up_states = thread_states[
                (thread_states['ts'] <= analysis_start) & 
                (thread_states['state'] != 'Running')
            ]
            if not wake_up_states.empty:
                wake_up_latency = wake_up_states['dur'].sum()

            sum_running = sum(cpu_running_time.values())
            big_core_running = sum(dur for cpu, dur in cpu_running_time.items() if int(cpu) >= 6)
            small_core_running = sum(dur for cpu, dur in cpu_running_time.items() if int(cpu) < 6)
            return {
                'sum_running': sum_running,
                'big_core_running': big_core_running,
                'small_core_running': small_core_running,
                'cpu_running_time': cpu_running_time,
                'total_runnable_time': total_runnable_time,
                'total_blocked_time': total_blocked_time,
                'total_sleeping_time': total_sleeping_time,
                'io_wait_duration': io_wait_duration,
                'wake_up_latency': wake_up_latency,
                'blocked_function_time_dict': blocked_function_time_dict,
                'sleep_waker_info': sleep_waker_info,
                'runnable_state_cpu_occupiers': runnable_state_cpu_occupiers
            }

        choreographer_analysis = analyze_thread_states(choreographer_states, exp_ts, recorddraw_end)
        before_chor_big_core_running = analyze_thread_states(choreographer_states, exp_ts, choreographer_start, running_only=True)['big_core_running']
        
        result_dict['chor_sum_running'] = choreographer_analysis['sum_running']
        result_dict['chor_big_core_running'] = choreographer_analysis['big_core_running']
        result_dict['chor_small_core_running'] = choreographer_analysis['small_core_running']
        result_dict['chor_cpu_running_time'] = choreographer_analysis['cpu_running_time']
        result_dict['chor_total_runnable_time'] = choreographer_analysis['total_runnable_time']
        result_dict['chor_total_blocked_time'] = choreographer_analysis['total_blocked_time']
        result_dict['chor_total_sleeping_time'] = choreographer_analysis['total_sleeping_time']
        result_dict['chor_io_wait_duration'] = choreographer_analysis['io_wait_duration']
        result_dict['chor_wake_up_latency'] = choreographer_analysis['wake_up_latency']
        result_dict['chor_blocked_function_time_dict'] = choreographer_analysis['blocked_function_time_dict']
        result_dict['chor_sleep_waker_info'] = choreographer_analysis['sleep_waker_info']
        result_dict['chor_runnable_state_cpu_occupiers'] = choreographer_analysis['runnable_state_cpu_occupiers']
        result_dict['before_chor_big_core_running'] = before_chor_big_core_running

        # Filter for big cores (CPU 6 and 7)
        big_core_df = locked_query_and_convert(f'''
        SELECT
            s.id,
            s.ts,
            s.dur,
            s.cpu,
            s.utid,
            th.tid,
            th.upid,
            pr.pid,                  -- 进程 PID
            th.is_main_thread,
            th.name  AS thread_name, -- 线程名
            pr.name  AS pid_name     -- 进程名
        FROM sched   AS s
        JOIN thread  AS th ON s.utid = th.utid
        JOIN process AS pr ON th.upid = pr.upid
        WHERE s.cpu IN (6, 7)
        AND s.ts <  {choreographer_end}
        AND s.ts + s.dur > {exp_ts}
        ''', "big_core_slices_query")


        df = big_core_df.copy()
        df['overlap_start'] = df['ts'].clip(lower=exp_ts)
        df['overlap_end']   = (df['ts'] + df['dur']).clip(upper=choreographer_end)
        df['overlap_dur']   = (df['overlap_end'] - df['overlap_start']).clip(lower=0)

        # 汇总
        summary_df = ( 
            df.groupby(['tid', 'pid_name', 'thread_name', 'is_main_thread'], as_index=False)['overlap_dur'] 
            .sum() 
            .rename(columns={'overlap_dur': 'run_ns'}) 
            .assign(run_ms=lambda x: x.run_ns/1e6) 
            .sort_values('run_ns', ascending=False) 
        ) 
        sum_running = summary_df['run_ns'].sum()
        summary_df['time_pct'] = (summary_df['run_ns'] / len(["cpu6", "cpu7"]) / (choreographer_end - exp_ts) * 100).apply(lambda x: round(x, 2))
        summary_df['run_pct'] = (summary_df['run_ns'] / sum_running * 100).apply(lambda x: round(x, 2))
        result_dict['chor_big_core_slices'] = summary_df
    except Exception as e:
        logger.exception(f"[Analyze Frame {frame_idx}] Error querying main thread slices/state: {e}")
    
    # --- 4. 检查 GPU/RenderThread 是否耗时过长 (原因类别 3) ---
    try:
        # 需要 render_thread_utid 和 drawframe 时间
        if render_thread_utid is None:
             raise ValueError("Render thread UTID is required for GPU analysis")
        if pd.isna(recorddraw_end) or pd.isna(drawframe_end):
             raise ValueError("DrawFrame times are required for GPU analysis")

        drawframe_dur = drawframe_end - drawframe_start

        # 查询 DrawFrame 时间段内，RenderThread 上的特定耗时 slice
        gpu_slices_query = f"""
        SELECT name, ts, dur
        FROM slice
        WHERE track_id = (SELECT id FROM thread_track WHERE utid = {render_thread_utid})
          AND ts >= {drawframe_start} AND ts < {drawframe_end}
          AND (name LIKE 'Texture upload%' OR name LIKE '%skgpu::v1::OpsTask::onExecute%')
        ORDER BY dur DESC
        """
        gpu_slices = locked_query_and_convert(gpu_slices_query, "gpu_slices_analysis")
        skgpu_duration = gpu_slices[gpu_slices['name'].str.contains('skgpu::v1::OpsTask::onExecute')]['dur'].sum() if not gpu_slices.empty else 0
        texture_upload_duration = gpu_slices[gpu_slices['name'].str.contains('Texture upload')]['dur'].sum() if not gpu_slices.empty else 0
        result_dict['skgpu_duration'] = skgpu_duration
        result_dict['texture_upload_duration'] = texture_upload_duration
        
        # 检查方法 2: 查看 DrawFrame 整个 slice 是否就过长
        thread_state_query = f"""
        SELECT ts, dur, cpu, state, io_wait, waker_utid
        FROM thread_state
        WHERE utid = {render_thread_utid}
            AND ts < {gpufence_end} -- END
            AND ts + dur > {recorddraw_end} -- START
        ORDER BY ts
        """
        thread_states = locked_query_and_convert(thread_state_query, "render_thread_states")
        render_thread_states = [copy.deepcopy(thread_states)]
        pre_render_thread_states = []
        render_thread_states = pd.concat(pre_render_thread_states + render_thread_states, ignore_index=True)
        
        render_thread_analysis = analyze_thread_states(
            render_thread_states, recorddraw_end, gpufence_end)
        
        result_dict['render_sum_running'] = render_thread_analysis['sum_running']
        result_dict['render_big_core_running'] = render_thread_analysis['big_core_running']
        result_dict['render_small_core_running'] = render_thread_analysis['small_core_running']
        result_dict['render_cpu_running_time'] = render_thread_analysis['cpu_running_time']
        result_dict['render_total_runnable_time'] = render_thread_analysis['total_runnable_time']
        result_dict['render_total_blocked_time'] = render_thread_analysis['total_blocked_time']
        result_dict['render_total_sleeping_time'] = render_thread_analysis['total_sleeping_time']
        result_dict['render_io_wait_duration'] = render_thread_analysis['io_wait_duration']
        result_dict['render_blocked_function_time_dict'] = render_thread_analysis['blocked_function_time_dict']
        result_dict['render_sleep_waker_info'] = render_thread_analysis['sleep_waker_info']
        result_dict['render_runnable_state_cpu_occupiers'] = render_thread_analysis['runnable_state_cpu_occupiers']
        frame_info_ts = frame_row['frame_info_ts']
        frame_info_dur = frame_row['frame_info_dur']
        total_frame_duration = frame_row['total_frame_duration']
        result_dict['ui_stage_start_latency'] = choreographer_start - exp_ts
        result_dict['total_ui_duration'] = recorddraw_end - choreographer_start
        result_dict['render_wake_up_latency'] = drawframe_start - recorddraw_end
        result_dict['total_draw_duration'] = gpufence_end - drawframe_start
        result_dict['total_gpu_stage_duration'] = (frame_info_ts + frame_info_dur) - gpufence_end
        result_dict['frame_stages_breakdown'] = describe_frame_stages(result_dict, total_frame_duration, exp_dur, frame_idx)
    except Exception as e:
        logger.warning(f"[Analyze Frame {frame_idx}] Error querying GPU slices: {e}")
    return result_dict



import pandas as pd
from collections import defaultdict
import logging
from typing import Dict, Tuple, Optional, Callable, List
import re


# --- Thresholds ---
MIN_SIGNIFICANT_DURATION_NS = 3_000_000       # 3ms: Minimum duration to be considered a factor
SIGNIFICANT_PERCENTAGE_THRESHOLD = 0.30     # 30%: Minimum percentage of relevant total time
MAJOR_CONTRIBUTOR_FACTOR = 1.8              # Factor A needs to be 80% larger than B to dominate it
GPU_COMPLETION_JANK_THRESHOLD_NS = 8_000_000 # 8ms: Example threshold for standalone GPU completion jank

# --- Helper: Type Definition for Rule Functions ---
# Takes metrics, returns score (duration) and category name if applicable
RuleCheckResult = Tuple[float, Optional[str]]
RuleChecker = Callable[[Dict[str, float], Dict[str, float], float, float], RuleCheckResult]

# --- Individual Cause Checker Functions ---

def get_big_core_description(big_core_slices) -> tuple[str, list]:
    """Generates a description for the big core slices."""
    ret_list = []
    if big_core_slices is None or big_core_slices.empty:
        return ""
    big_core_slices_desc = ''
    for _, row in big_core_slices.iterrows():
        if row['run_pct'] < 10:
            continue
        ret_list.append((row['pid_name'], row['thread_name'], row['run_ms']))
        name = row['pid_name'].split('/')[-1] + '/' + row['thread_name']
        big_core_slices_desc += f"{name} ({row['run_ms']:.2f}ms) + "
    return big_core_slices_desc[:-3], ret_list

def check_input_bound(durations: Dict[str, float], percentages: Dict[str, float],
                      total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks if Input handling is a likely cause."""
    input_dur = durations.get("total_input_duration", 0)
    input_pct = percentages.get("total_input_duration", 0)
    if input_dur > MIN_SIGNIFICANT_DURATION_NS and input_pct > SIGNIFICANT_PERCENTAGE_THRESHOLD:
        reason = f"Input Bound - {input_dur / 1E6 :.2f}ms"
        # Optional: Add more nuanced checks here, e.g., comparing vs Animation
        if durations.get("chor_small_core_running", 0) > input_dur * 0.5: # Example constraint
            big_core_slices = durations.get("chor_big_core_slices", pd.DataFrame())
            desc, lst = get_big_core_description(big_core_slices)
            return input_dur, f"{reason} (Small Core) ({desc})", lst
        return input_dur, reason
    return 0, None

def check_animation_bound(durations: Dict[str, float], percentages: Dict[str, float],
                          total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks if Animation is a likely cause."""
    anim_dur = durations.get("animation_total_dur", 0)
    anim_pct = percentages.get("animation_total_dur", 0)
    if anim_dur > MIN_SIGNIFICANT_DURATION_NS and anim_pct > SIGNIFICANT_PERCENTAGE_THRESHOLD:
        # Optional: Ensure Input wasn't also very high but missed its check
        # input_dur = durations.get("total_input_duration", 0)
        # if input_dur < anim_dur * 0.5: # Example constraint
        reason = f"Animation Bound - {anim_dur / 1E6 :.2f}ms"
        if durations.get("chor_small_core_running", 0) > anim_dur * 0.5: # Example constraint
            big_core_slices = durations.get("chor_big_core_slices", pd.DataFrame())
            desc, lst = get_big_core_description(big_core_slices)
            return anim_dur, f"{reason} (Small Core) ({desc})", lst
        else:
            return anim_dur, reason
    return 0, None

def check_cpu_scheduling(durations: Dict[str, float], percentages: Dict[str, float],
                         total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks for significant time spent in Runnable state."""
    runnable_dur = durations.get("chor_total_runnable_time", 0)
    runnable_pct = percentages.get("Runnable (vs UI)", 0)
    if runnable_dur > MIN_SIGNIFICANT_DURATION_NS and runnable_pct > 0.1:
        return runnable_dur, f"CPU Scheduling Delay - {runnable_dur / 1E6 :.2f}ms"
    return 0, None

def check_blocked_state(durations: Dict[str, float], percentages: Dict[str, float],
                        total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks for significant time spent in Blocked state."""
    blocked_dur = durations.get("chor_total_blocked_time", 0)
    blocked_pct = percentages.get("Blocked (vs UI)", 0)
    io_wait_dur = durations.get("chor_io_wait_duration", 0)

    if blocked_dur > MIN_SIGNIFICANT_DURATION_NS and blocked_pct > 0.1:
        if io_wait_dur > blocked_dur * 0.5: # Check if IO Wait is the primary reason for blocking
             return blocked_dur, f"Blocked State (IO Wait) - {blocked_dur / 1E6 :.2f}ms"
        else:
             return blocked_dur, f"Blocked State (Lock/Other) - {blocked_dur / 1E6 :.2f}ms"
    return 0, None

def check_draw_gpu_upload(durations: Dict[str, float], percentages: Dict[str, float],
                          total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks for bottlenecks within the Draw phase (skgpu, Texture Upload)."""
    skgpu_dur = durations.get("skgpu_duration", 0)
    texture_dur = durations.get("texture_upload_duration", 0)
    draw_gpu_upload_dur = durations.get("Draw (Skgpu/Upload)", 0)
    draw_gpu_upload_pct = percentages.get("Draw (Skgpu/Upload) (vs Draw)", 0)
    draw_phase_vs_ui_pct = percentages.get("total_draw_duration", 0)
    draw_phase_dur = durations.get("total_draw_duration", 0)
    if total_draw_duration > MIN_SIGNIFICANT_DURATION_NS and \
        draw_phase_vs_ui_pct > SIGNIFICANT_PERCENTAGE_THRESHOLD:
        detail = []
        if durations.get("render_small_core_running", 0) > draw_phase_dur * 0.5: # Example constraint
            return draw_phase_dur, f"Draw Bound (Small Core) - {draw_phase_dur / 1E6:.2f}ms"
        # Check significance relative *within* the draw phase:
        if percentages.get("skgpu (vs Draw)", 0) > SIGNIFICANT_PERCENTAGE_THRESHOLD * 0.8:
            detail.append(f"skgpu - {skgpu_dur / 1E6:.2f}ms")
        if percentages.get("Texture Upload (vs Draw)", 0) > SIGNIFICANT_PERCENTAGE_THRESHOLD * 0.8:
            detail.append(f"Texture Upload - {texture_dur / 1E6:.2f}ms")
        if detail:
            return draw_phase_dur, f"Draw Bound {draw_phase_dur / 1E6:.2f}ms ({', '.join(detail)})"
        else:
            if draw_gpu_upload_dur > MIN_SIGNIFICANT_DURATION_NS and \
               draw_gpu_upload_pct > SIGNIFICANT_PERCENTAGE_THRESHOLD:
                return draw_phase_dur, f"Draw Bound {draw_phase_dur / 1E6:.2f}ms (GPU/Upload Heavy) - {draw_gpu_upload_dur / 1E6:.2f}ms"
            else:
                return draw_phase_dur, f"Draw Bound {draw_phase_dur / 1E6:.2f}ms (Overall Phase) - {draw_phase_dur / 1E6:.2f}ms"
    return 0, None

# --- NEW RULE Example: GPU Completion ---
def check_gpu_completion(durations: Dict[str, float], percentages: Dict[str, float],
                         total_frame_duration: float, total_draw_duration: float) -> RuleCheckResult:
    """Checks if waiting for GPU completion took excessively long."""
    gpu_completion_dur = durations.get("gpucompletion_dur", 0) # Assume features has 'gpu_completion_duration'

    # GPU completion is often independent; check against an absolute threshold or VSync interval
    # Here, using an absolute threshold example:
    if gpu_completion_dur > durations.get("total_draw_duration", 0) + durations.get("total_ui_duration", 0):
        return gpu_completion_dur, f"GPU Completion Bound - {gpu_completion_dur / 1E6:.2f}ms"
    return 0, None

# --- List of all rule checker functions ---
# Order matters slightly for tie-breaking if scores are identical, but primary logic relies on score magnitude
ALL_RULE_CHECKERS: List[RuleChecker] = [
    check_input_bound,
    check_animation_bound,
    check_cpu_scheduling,
    check_blocked_state,
    check_draw_gpu_upload,         # Check specific draw content first
    check_gpu_completion,          # Check GPU completion wait
    # Add new rule functions here
]

def classify_jank_cause(frame_row: pd.Series, durations: dict, big_core_occupiers_l, logger) -> tuple[str, str]:
    """
    Classifies the cause of a self-jank frame using an extensible rule-based system.

    Args:
        frame_row: A pandas Series containing frame timestamps and info.

        e.g. 
        ts,vsync_app_ts,choreographer_start,choreographer_end,recorddraw_start,recorddraw_end,drawframe_start,drawframe_end,gpufence_start,gpufence_end,gpucompletion_start,gpucompletion_end,vsync_sf_ts,deadline,is_drop,rendering_time,frame_idx,frame_info_ts,frame_info_dur,frame_info_jank_type,frame_info_jank_tag,present_type,on_time_finish,gpu_composition,prediction_type,layer_name,jank_cause_category,jank_main_reason
        6349779212708,6349778716563,6349779212708,6349796010679,6349781245365,6349782160313,6349794343908,6349808757451,6349808563545,6349808577659,6349809797243,6349819054952,6349795231460,6349811898126,False,29364951,2601058,6349779214791.0,39759327.0,Buffer Stuffing,Buffer Stuffing,Late Present,1.0,0.0,Valid Prediction,TX - com.zhiliaoapp.musically/com.ss.android.ugc.aweme.splash.SplashActivity#510,Not Analyzed,Not Analyzed

        features: A dictionary containing duration metrics for the frame.
                  Expected to potentially contain 'gpu_completion_duration'.
        main_thread_utid: UTID of the main thread.
        render_thread_utid: UTID of the render thread.

    Returns:
        A string category describing the likely jank cause.
    """

    # --- 1. Calculate Common Metrics ---

    total_frame_duration = frame_row['total_frame_duration']
    total_draw_duration = durations.get('total_draw_duration', 0)
    total_ui_duration = durations.get('total_ui_duration', 0)
    # logger.info(durations)
    # --- 2. Prepare Durations and Percentages Dicts ---
    durations["Draw (Skgpu/Upload)"] = durations["skgpu_duration"] + durations["texture_upload_duration"]

    percentages = {}
    if total_frame_duration > 0:
        percentages["total_input_duration"] = durations["total_input_duration"] / total_frame_duration
        percentages["animation_total_dur"] = durations["animation_total_dur"] / total_frame_duration
        if total_draw_duration > 0 :
             percentages["total_draw_duration"] = total_draw_duration / total_frame_duration
    if total_ui_duration > 0:
        percentages["Runnable (vs UI)"] = durations["chor_total_runnable_time"] / total_ui_duration
        percentages["Blocked (vs UI)"] = durations["chor_total_blocked_time"] / total_ui_duration

    if total_draw_duration > 0:
         percentages["skgpu (vs Draw)"] = durations["skgpu_duration"] / total_draw_duration
         percentages["Texture Upload (vs Draw)"] = durations["texture_upload_duration"] / total_draw_duration
         percentages["Draw (Skgpu/Upload) (vs Draw)"] = durations["Draw (Skgpu/Upload)"] / total_draw_duration

    # --- 3. Run All Rule Checkers ---
    rule_results: List[Tuple[float, str]] = []
    for checker in ALL_RULE_CHECKERS:
        ret = checker(durations, percentages, total_frame_duration, total_draw_duration)
        if len(ret) == 2:
            score, category = ret
        else:
            score, category, big_core_occupiers = ret
            big_core_occupiers_l.append(big_core_occupiers)
        if category and score > 0:
            rule_results.append((score, category))

    # --- 4. Analyze Results ---
    if not rule_results:
        if durations["total_input_duration"] > 0:
            logger.debug(f"Frame {frame_row.get('frame_idx', 'N/A')}: No specific rule triggered. Input duration: {durations['total_input_duration'] / 1e6:.2f}ms.")
            return f"Input Bound (No Rule Triggered) - {durations['total_input_duration'] / 1e6:.2f}ms", "Input Bound"
        # If no rule triggered, find the single largest raw duration as a fallback clue
        all_durs_sorted = sorted(
            [(k, v) for k, v in durations.items() if str(v).isnumeric() and v > 0 and k != "Draw (Skgpu/Upload)"],
             key=lambda item: item[1], reverse=True
        )
        if all_durs_sorted:
             top_cause, _ = all_durs_sorted[0]
             logger.debug(f"Frame {frame_row.get('frame_idx', 'N/A')}: No specific rule triggered. Largest raw contributor: {top_cause}. "
                          f"Durations: {durations} Percentages: {percentages}")
             return f"Other/Unknown (Largest: {top_cause})", "Other"
        else:
             logger.debug(f"Frame {frame_row.get('frame_idx', 'N/A')}: No significant durations found by any rule.")
             return "Other/Unknown", "Other"

    # Sort results by score (duration) descending
    rule_results.sort(key=lambda item: item[0], reverse=True)

    top_score, top_category = rule_results[0]

    def classify(primary):
        if "Small Core" in primary:
            return "Small Core"
        if primary.startswith("Input Bound"):
            return "Input Bound"
        if primary.startswith("Draw Bound"):
            return "Draw Bound"
        if primary.startswith("Animation Bound"):
            return "Animation Bound"
        if primary.startswith("GPU Completion Bound"):
            return "GPU completion"
        return "Other"
    top_main_reason = classify(top_category)

    # Check for a single dominant cause
    if len(rule_results) == 1:
        return f"{top_category} (Dominant)", top_main_reason

    # Check if the top cause significantly outweighs the second
    second_score, second_category = rule_results[1]
    if top_score >= second_score * MAJOR_CONTRIBUTOR_FACTOR:
        # Optional: Check if adding second category makes sense despite dominance factor
        # e.g., if second_score is still very large absolutely or percentage-wise
        # For now, declare dominant based on factor:
        logger.debug(f"Frame {frame_row.get('frame_idx', 'N/A')}: Top cause {top_category} ({top_score/1e6:.2f}ms) dominates "
                     f"second cause {second_category} ({second_score/1e6:.2f}ms).")
        return f"{top_category} (Dominant)", top_main_reason
    else:
        # Multiple potential causes - list the major contributors
        # Define "major" as being within a certain range of the top score (e.g., > 50% of top score)
        major_contributors = [cat for score, cat in sorted(rule_results, key=lambda item: item[0], reverse=True) if score >= top_score * 0.5]
        logger.debug(f"Frame {frame_row.get('frame_idx', 'N/A')}: Multiple contributors found: {rule_results}")
        return f"Multiple Causes ({' + '.join(major_contributors)})", top_main_reason


def describe_blocked_functions(blocked_function_dict: dict) -> str:
    """
    将阻塞函数字典转换为描述字符串。
    """
    if not blocked_function_dict:
        return "No blocked functions"
    sum_dur = sum(blocked_function_dict.values())
    if sum_dur == 0:
        return "No blocked functions"
    desc = ""
    for func, dur in blocked_function_dict.items():
        if dur > 0:
            desc += f"{func} ({dur/1E6:.2f}ms; {dur/sum_dur*100:.1f}%); "
    return desc[:-2] if desc else "No significant blocked functions"

def describe_sleep_waker_info(sleep_waker_dict: dict) -> str:
    """
    将sleep waker字典转换为描述字符串。
    """
    if not sleep_waker_dict:
        return "No sleep waker info"
    sum_dur = sum(sleep_waker_dict.values())
    if sum_dur == 0:
        return "No sleep waker info"
    desc = ""
    for waker_name, dur in sleep_waker_dict.items():
        if dur > 0:
            desc += f"{waker_name} ({dur/1E6:.2f}ms; {dur/sum_dur*100:.1f}%); "
    return desc[:-2] if desc else "No significant sleep waker info"


class DummyLock:
    """A dummy lock that doesn't actually lock anything, for single-threaded scenarios."""
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
    
    def acquire(self):
        pass
    
    def release(self):
        pass
def classify_jank(df, trace_path, main_proc_pid, target_process, logger):
    # df: like perfetto-com.zhiliaoapp.musically-3543-1750666803.798253.csv
    # main_proc_pid: extracted from trace file, like perfetto-com.zhiliaoapp.musically-3543-1750666803.798253.trace, e.g. 3543
    # target_process: extracted from trace file, like com.zhiliaoapp.musically
    
    # 创建 TraceProcessor 实例池 - 使用多线程并行创建
    logger.debug(f"Creating TraceProcessor pool with {tp_pool_size} instances...")
    tp_pool_start = time.time()
    
    def create_tp_instance(index):
        """创建单个TraceProcessor实例的工作函数"""
        tp = TraceProcessor(
            trace=trace_path, 
            config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
        )
        if tp_pool_size >= max_workers:
            lock =DummyLock()
        else:
            lock = Lock()
        logger.debug(f"TraceProcessor instance {index} created")
        return tp, lock
    
    # 并行创建TraceProcessor实例
    tp_pool = [None] * tp_pool_size  # 预分配列表保持顺序
    tp_locks = [None] * tp_pool_size
    with concurrent.futures.ThreadPoolExecutor(max_workers=tp_pool_size) as executor:
        # 提交建任务
        future_to_index = {executor.submit(create_tp_instance, i): i for i in range(tp_pool_size)}
        
        # 收集结果，确保按索引顺序存储
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                tp, lock = future.result()
                tp_pool[index] = tp
                tp_locks[index] = lock
            except Exception as e:
                logger.error(f"Failed to create TraceProcessor instance {index}: {e}")
                # 创建备用实例
                tp = TraceProcessor(
                    trace=trace_path, 
                    config=TraceProcessorConfig(bin_path=os.path.expanduser('~/.local/bin/trace_processor'))
                )
                tp_pool[index] = tp
                tp_locks[index] = Lock()
        
    tp_pool_init_time = time.time() - tp_pool_start
    logger.debug(f"TraceProcessor pool ({len(tp_pool)} instances) created in {tp_pool_init_time:.3f}s")
    
    # 创建线程本地存储的索引
    import threading
    thread_local = threading.local()
    tp_index_counter = 0
    tp_index_lock = Lock()
    
    def get_tp_for_thread():
        """为当前线程分配一个 TraceProcessor 实例"""
        nonlocal tp_index_counter
        
        if not hasattr(thread_local, 'tp_index'):
            with tp_index_lock:
                thread_local.tp_index = tp_index_counter % tp_pool_size
                tp_index_counter += 1
        
        return tp_pool[thread_local.tp_index], tp_locks[thread_local.tp_index]
    
    # 创建分布式查询函数
    def distributed_query_and_convert(sql: str, location: str = "unknown"):
        tp, lock = get_tp_for_thread()
        with lock:
            return tp.query(sql).as_pandas_dataframe()
    # 如果没有传入 PID，尝试从文件名提取
    if main_proc_pid is None:
        pid_match = re.search(r'perfetto-.*-(\d+)-\d+\.\d+\.trace', os.path.basename(trace_path))
        if pid_match:
            main_proc_pid = int(pid_match.group(1))
            logger.debug(f"Extracted PID from trace file name: {main_proc_pid}")
        else:
            # 尝试从trace数据库中查找PID（使用包名）
            if target_process and target_process != 'none':
                try:
                    find_pid_query = f"""
                    SELECT pid FROM process
                    WHERE name = '{target_process}'
                    ORDER BY pid DESC
                    LIMIT 1
                    """
                    pid_result = distributed_query_and_convert(find_pid_query, "find_pid_from_package")
                    if not pid_result.empty:
                        main_proc_pid = int(pid_result['pid'].iloc[0])
                        logger.info(f"Found PID {main_proc_pid} for package {target_process} from trace database")
                    else:
                        logger.warning(f"Could not find PID for package {target_process} in trace database, skipping jank classification")
                        return df, {}, {}, {}, {}, {}, {}  # 返回空结果，跳过 jank 分类
                except Exception as e:
                    logger.warning(f"Error querying PID from trace database: {e}, skipping jank classification")
                    return df, {}, {}, {}, {}, {}, {}  # 返回空结果，跳过 jank 分类
            else:
                logger.warning(f"Could not extract PID from trace file name: {trace_path} and no valid target_process provided, skipping jank classification")
                return df, {}, {}, {}, {}, {}, {}  # 返回空结果，跳过 jank 分类
    else:
        logger.debug(f"Using provided PID: {main_proc_pid}")

    main_thread_utid: Optional[int] = None
    render_thread_utid: Optional[int] = None
    main_thread_tid: Optional[int] = None
    render_thread_tid: Optional[int] = None # RenderThread 的 TID 也可能有用

    # 查找主线程 UTID
    try:
        find_main_process_query = f"""
        select upid, pid, name from process
        WHERE pid = {main_proc_pid}
        LIMIT 1
        """
        # logger.info(f"Executing query to find main process: {find_main_process_query}")
        main_process_info = distributed_query_and_convert(find_main_process_query, "find_main_process")
        
        find_main_thread_query = f"""
        SELECT tid, utid, name FROM thread
        WHERE tid = {main_proc_pid} 
        LIMIT 1
        """

        # logger.info(f"Executing query to find main thread: {find_main_thread_query}")
        main_thread_info = distributed_query_and_convert(find_main_thread_query, "find_main_thread")

        if not main_thread_info.empty and not main_process_info.empty:
            main_thread_tid = main_thread_info['tid'].iloc[0]
            main_thread_utid = main_thread_info['utid'].iloc[0]
            main_process_upid = main_process_info['upid'].iloc[0]
            target_thread = main_thread_info['name'].iloc[0]
            logger.debug(f"Found Main Thread: name='{target_thread}', tid={main_thread_tid}, utid={main_thread_utid}")
        else:
            logger.warning(f"Could not automatically determine Main Thread TID/UTID for '{target_process}' using query. Jank analysis might be limited.")
            # 即使找不到，后续查询仍会尝试使用名称匹配，但可能不精确

    except Exception as e:
        logger.error(f"Error finding Main Thread UTID: {e}")

    # 查找 RenderThread UTID
    render_thread_name = 'RenderThread' # 通常是这个名字
    try:
        find_render_thread_query = f"""
        SELECT tid, utid FROM thread
        WHERE name = '{render_thread_name}'
            AND upid = {main_process_upid}
        LIMIT 1
        """
        render_thread_info = distributed_query_and_convert(find_render_thread_query, "find_render_thread")

        if not render_thread_info.empty:
            render_thread_tid = render_thread_info['tid'].iloc[0]
            render_thread_utid = render_thread_info['utid'].iloc[0]
            logger.debug(f"Found RenderThread: tid={render_thread_tid}, utid={render_thread_utid}")
        else:
            logger.warning(f"Could not find RenderThread UTID for '{target_process}'. GPU Jank analysis will be limited.")

    except Exception as e:
        logger.error(f"Error finding RenderThread UTID: {e}")


    df['frame_info_jank_tag'].unique()

    # 添加新列用于存储分析结果
    df['jank_cause_category'] = "Not Analyzed"
    df['jank_main_reason'] = "Not Analyzed"
    df['sleep_waker_info'] = "Not Analyzed"
    # 使用 .str.contains() 并处理 NaN 值 (如果 frame_timeline 查询失败或未匹配)
    # 确保 frame_info_jank_type 存在
    self_jank_mask = pd.Series(False, index=df.index) # Default to False
    if 'frame_info_jank_tag' in df.columns:
        # Fill NaN with empty string before checking contains
        self_jank_mask = df['frame_info_jank_tag'].fillna('').str.contains("Self Jank")
    big_core_occupiers_ls = []
    self_jank_frames = df[self_jank_mask]
    # select first 10 self-jank frames for analysis
    if False and len(self_jank_frames) > 10:
        self_jank_frames = self_jank_frames.head(10)
        logger.info("Limiting analysis to first 10 Self Jank frames for performance reasons.")
    num_self_jank = len(self_jank_frames)
    logger.info(f"Found {num_self_jank} frames marked as 'Self Jank' by Perfetto.")
    if num_self_jank > 0:
        if main_thread_utid is None or render_thread_utid is None:
            logger.warning("Cannot perform detailed jank analysis because Main Thread UTID or Render Thread UTID for analysis is missing.")
            # 将所有 self-jank 帧标记为跳过分析
            df.loc[self_jank_mask, 'jank_cause_category'] = "Analysis Skipped (Missing UTID)"
        else:
            logger.debug("Starting detailed analysis of Self-Jank frames...")

            # 使用默认的CPU核心数，如果没有指定max_workers
            
            global_cpu_state_summary = CPUStateSummary()
            global_blocked_function = defaultdict(int)
            global_sleep_waker = defaultdict(int)
            global_runnable_state_cpu_occupiers = []
            analyzed_count = 0
            
            # 准备并行处理的参数
            frame_tasks = []
            nr_work = 0
            for index, frame_row in self_jank_frames.iterrows():
                frame_tasks.append((tp_pool[nr_work % tp_pool_size], tp_locks[nr_work % tp_pool_size], frame_row, index, main_thread_utid, render_thread_utid, logger.level))
                nr_work += 1
            
            logger.debug(f"Processing {len(frame_tasks)} self-jank frames using {max_workers} worker threads...")
            
            # 重试配置
            max_retries = 2  # 最多重试2次
            retry_delay = 0.1  # 重试延迟（秒）
            
            # 使用线程池执行器并行处理
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # 提交所有任务
                future_to_task = {executor.submit(analyze_single_frame_worker, task): task for task in frame_tasks}
                
                # 收集结果和失败任务
                results = {}
                failed_tasks = []
                
                for future in concurrent.futures.as_completed(future_to_task):
                    task = future_to_task[future]
                    index = task[2]  # task[2] 是 index
                    
                    try:
                        result = future.result()
                        results[result['frame_index']] = result
                        analyzed_count += 1
                        
                        if analyzed_count % 10 == 0:  # 每处理10个帧记录一次进度
                            logger.debug(f"Processed {analyzed_count}/{len(frame_tasks)} frames...")
                        
                    except Exception as e:
                        logger.exception(f"Frame at index {index} failed on first attempt: {e}")
                        failed_tasks.append(task)
                
                # 重试失败的任务
                for retry_attempt in range(max_retries):
                    if not failed_tasks:
                        break
                        
                    logger.info(f"Retrying {len(failed_tasks)} failed frames (attempt {retry_attempt + 1}/{max_retries})...")
                    
                    # 为重试任务重新提交
                    retry_future_to_task = {}
                    for task in failed_tasks:
                        # 添加重试延迟
                        time.sleep(retry_delay)
                        retry_future_to_task[executor.submit(analyze_single_frame_worker, task)] = task
                    
                    # 收集重试结果
                    retry_failed_tasks = []
                    for future in concurrent.futures.as_completed(retry_future_to_task):
                        task = retry_future_to_task[future]
                        index = task[2]
                        
                        try:
                            result = future.result()
                            results[result['frame_index']] = result
                            analyzed_count += 1
                            logger.info(f"Frame at index {index} succeeded on retry attempt {retry_attempt + 1}")
                            
                        except Exception as e:
                            logger.warning(f"Frame at index {index} failed on retry attempt {retry_attempt + 1}: {e}")
                            retry_failed_tasks.append(task)
                    
                    failed_tasks = retry_failed_tasks
                
                # 为最终失败的任务创建默认结果
                for task in failed_tasks:
                    index = task[2]
                    logger.error(f"Frame at index {index} failed after all retry attempts")
                    results[index] = {
                        'frame_index': index,
                        'category': "Analysis Failed",
                        'main_reason': "Analysis Failed",
                        'cpu_state_summary': CPUStateSummary(),
                        'cpu_state_summary_str': "Analysis Failed",
                        'blocked_function_time_dict': "Analysis Failed",
                        'sleep_waker_info': "Analysis Failed",
                        'local_block_function': {},
                        'local_sleep_waker': {},
                        'big_core_occupiers': [],
                        'local_runnable_state_cpu_occupiers': [],
                        'local_runnable_state_analysis': "Analysis Failed"
                    }
                
                final_failed_count = len(failed_tasks)
                if final_failed_count > 0:
                    logger.warning(f"Final summary: {final_failed_count} frames failed analysis after all retries")
            
            # 将结果写回主 DataFrame 并汇总全局统计
            for index, frame_row in self_jank_frames.iterrows():
                if index in results:
                    result = results[index]
                    
                    # 写回DataFrame
                    df.loc[index, 'jank_cause_category'] = result['category']
                    df.loc[index, 'jank_main_reason'] = result['main_reason']
                    df.loc[index, 'cpu_state_summary'] = result['cpu_state_summary_str']
                    df.loc[index, 'blocked_function_time_dict'] = result['blocked_function_time_dict']
                    df.loc[index, 'sleep_waker_info'] = result['sleep_waker_info']
                    df.loc[index, 'runnable_state_info'] = result['local_runnable_state_analysis']
                    df.loc[index, 'frame_stages_breakdown'] = result['frame_stages_breakdown']
                    
                    # 汇总全局统计
                    global_cpu_state_summary.update(result['cpu_state_summary'])
                    if len(result['big_core_occupiers']) > 0:
                        big_core_occupiers_ls += result['big_core_occupiers']
                    
                    # 汇总全局阻塞函数和睡眠唤醒信息
                    for block_func, dur in result['local_block_function'].items():
                        global_blocked_function[block_func] += dur
                    for waker_name, dur in result['local_sleep_waker'].items():
                        global_sleep_waker[waker_name] += dur
                    global_runnable_state_cpu_occupiers += result['local_runnable_state_cpu_occupiers']
                    

            logger.info(f"Finished analyzing {analyzed_count} self-jank frames.")
        # sort global_blocked_function by duration
        global_blocked_function = dict(sorted(global_blocked_function.items(), key=lambda item: item[1], reverse=True))
        global_sleep_waker = dict(sorted(global_sleep_waker.items(), key=lambda item: item[1], reverse=True))
        global_frame_stages = {
            'ui_stage_start_latency': sum(result.get('features', {}).get('ui_stage_start_latency', 0) for result in results.values()),
            'total_ui_duration': sum(result.get('features', {}).get('total_ui_duration', 0) for result in results.values()),
            'render_wake_up_latency': sum(result.get('features', {}).get('render_wake_up_latency', 0) for result in results.values()),
            'total_draw_duration': sum(result.get('features', {}).get('total_draw_duration', 0) for result in results.values()),
            'total_gpu_stage_duration': sum(result.get('features', {}).get('total_gpu_stage_duration', 0) for result in results.values()),
        }
        global_total_frame_dur = self_jank_frames['total_frame_duration'].sum()
        global_total_exp_dur = self_jank_frames['frame_info_dur'].sum()
        global_frame_stage_breakdown = describe_frame_stages(
            global_frame_stages, global_total_frame_dur, global_total_exp_dur, 0
        )
    return df, big_core_occupiers_ls, global_cpu_state_summary, global_blocked_function, global_sleep_waker, global_runnable_state_cpu_occupiers, global_frame_stage_breakdown

class CPUStateSummary:
    """
    CPU状态统计类，用于记录和展示CPU运行状态的统计信息。
    """
    
    def __init__(self):
        self.sum_running = 0
        self.big_core_running = 0
        self.small_core_running = 0
        self.runnable_time = 0
        self.sleep_time = 0
        self.blocked_time = 0
        self.gpu_phase = 0
        self.before_chor_big_core_running = 0
    
    def update(self, other):
        """
        更新当前统计数据，合并另一个 CPUStateSummary 实例的数据。
        """
        self.sum_running += other.sum_running
        self.big_core_running += other.big_core_running
        self.small_core_running += other.small_core_running
        self.runnable_time += other.runnable_time
        self.sleep_time += other.sleep_time
        self.blocked_time += other.blocked_time
        self.gpu_phase += other.gpu_phase
        self.before_chor_big_core_running += other.before_chor_big_core_running
    
    def to_dict(self) -> dict:
        """返回统计数据的字典表示"""
        return {
            "sum_running": self.sum_running,
            "big_core_running": self.big_core_running,
            "small_core_running": self.small_core_running,
            "runnable_time": self.runnable_time,
            "sleep_time": self.sleep_time,
            "blocked_time": self.blocked_time,
            "gpu_phase": self.gpu_phase,
            "main_thread_work": self.before_chor_big_core_running
        }
    
    def __str__(self) -> str:
        """
        返回时间值和百分比的字符串表示。
        """
        total_time = self.sum_running + self.runnable_time + self.sleep_time + self.blocked_time
        if total_time == 0:
            return "No CPU state data available"
        
        result = []
        
        # 运行时间统计
        if self.sum_running > 0:
            running_pct = (self.sum_running / total_time) * 100
            result.append(f"Running: {self.sum_running/1e6:.2f}ms ({running_pct:.1f}%)")
            
            if self.big_core_running > 0:
                big_pct = (self.big_core_running / self.sum_running) * 100
                result.append(f"  Big Core: {self.big_core_running/1e6:.2f}ms ({big_pct:.1f}%)")
            
            if self.small_core_running > 0:
                small_pct = (self.small_core_running / self.sum_running) * 100
                result.append(f"  Small Core: {self.small_core_running/1e6:.2f}ms ({small_pct:.1f}%)")
            
            if self.before_chor_big_core_running > 0:
                before_chor_pct = (self.before_chor_big_core_running / self.sum_running) * 100
                result.append(f"  Before Chor: {self.before_chor_big_core_running/1e6:.2f}ms ({before_chor_pct:.1f}%)")
        
        # 可运行时间统计
        if self.runnable_time > 0:
            runnable_pct = (self.runnable_time / total_time) * 100
            result.append(f"Runnable: {self.runnable_time/1e6:.2f}ms ({runnable_pct:.1f}%)")
        
        if self.blocked_time > 0:
            wait_pct = (self.blocked_time / total_time) * 100
            result.append(f"Block: {self.blocked_time/1e6:.2f}ms ({wait_pct:.1f}%)")
        
        # 睡眠时间统计
        if self.sleep_time > 0:
            sleep_pct = (self.sleep_time / total_time) * 100
            result.append(f"Sleep: {self.sleep_time/1e6:.2f}ms ({sleep_pct:.1f}%)")
        
        ret = ""
        
        if self.gpu_phase > 0:
            total_time_with_gpu = total_time + self.gpu_phase
            gpu_pct = (self.gpu_phase / total_time_with_gpu) * 100
            cpu_pct = (total_time / total_time_with_gpu) * 100
            ret += f"CPU Phase: {total_time/1e6:.2f}ms ({cpu_pct:.1f}%); "
        else:
            ret += f"CPU Phase: {total_time/1e6:.2f}ms (100%); "

        
        return ret + "; ".join(result) if result else "No significant CPU state data"

# @profile  # 注释掉profile装饰器，避免语法错误
def analyze_single_frame_worker(args):
    """
    Worker函数，用于多线程处理单个帧的分析
    
    Args:
        args: 包含分析所需参数的元组 (tp_pool, tp_lock, frame_row, frame_index, main_thread_utid, render_thread_utid, logger_level)
    
    Returns:
        包含分析结果的字典
    """
    assigned_tp, assigned_lock, frame_row, frame_index, main_thread_utid, render_thread_utid, logger_level = args
    
    # 为当前线程分配一个 TraceProcessor 实例和对应的锁
    import threading
    thread_id = threading.current_thread().ident
    
    # 创建线程本地的logger
    local_logger = logging.getLogger(f'worker_{thread_id}')
    local_logger.setLevel(logger_level)
    
    try:
        local_logger.debug(f"Analyzing self jank frame with index {frame_index}, frame_idx {frame_row['frame_idx']} using TP instance {assigned_tp}...")
        
        # Step 2: 调用分析函数，使用分配的 tp 实例和对应的锁
        features = analyze_single_jank_frame(assigned_tp, assigned_lock, frame_row, main_thread_utid, render_thread_utid, local_logger)
        
        # Step 3: 计算local CPU状态摘要
        local_cpu_state_summary = CPUStateSummary()
        local_cpu_state_summary.sum_running = features.get('chor_sum_running', 0) + features.get('render_sum_running', 0)
        local_cpu_state_summary.big_core_running = features.get('chor_big_core_running', 0) + features.get('render_big_core_running', 0)
        local_cpu_state_summary.small_core_running = features.get('chor_small_core_running', 0) + features.get('render_small_core_running', 0)
        local_cpu_state_summary.runnable_time = features.get('chor_total_runnable_time', 0) + features.get('render_total_runnable_time', 0)
        local_cpu_state_summary.blocked_time = features.get('chor_total_blocked_time', 0) + features.get('render_total_blocked_time', 0)
        local_cpu_state_summary.sleep_time = features.get('chor_total_sleeping_time', 0) + features.get('render_total_sleeping_time', 0)
        local_cpu_state_summary.gpu_phase = features.get('total_gpu_stage_duration', 0)
        local_cpu_state_summary.before_chor_big_core_running = features.get('before_chor_big_core_running', 0)
        
        # Step 4: 分类jank原因
        big_core_occupiers_l = []
        category, main_reason = classify_jank_cause(
            frame_row,
            features,
            big_core_occupiers_l,
            logger=local_logger
        )
        
        # Step 5: 处理阻塞函数和睡眠唤醒信息
        local_block_function = defaultdict(int)
        local_sleep_waker = defaultdict(int)
        local_runnable_state_cpu_occupiers = features.get('chor_runnable_state_cpu_occupiers', []) + features.get('render_runnable_state_cpu_occupiers', [])
        
        for block_func, dur in features.get('chor_blocked_function_time_dict', {}).items():
            local_block_function[block_func] += dur
        for block_func, dur in features.get('render_blocked_function_time_dict', {}).items():
            local_block_function[block_func] += dur
        for waker_name, dur in features.get('chor_sleep_waker_info', {}).items():
            local_sleep_waker[waker_name] += dur
        for waker_name, dur in features.get('render_sleep_waker_info', {}).items():
            local_sleep_waker[waker_name] += dur
        
        local_block_function_desc = describe_blocked_functions(local_block_function)
        local_sleep_waker_desc = describe_sleep_waker_info(local_sleep_waker)
        
        local_block_function = dict(sorted(local_block_function.items(), key=lambda item: item[1], reverse=True))
        local_sleep_waker = dict(sorted(local_sleep_waker.items(), key=lambda item: item[1], reverse=True))
        
        # Step 6: 处理runnable state分析
        if len(local_runnable_state_cpu_occupiers):
            local_runnable_state_analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(
                local_runnable_state_cpu_occupiers
            )
            local_runnable_state_analyzer.run_analysis()
            local_runnable_state_desc = local_runnable_state_analyzer.get_compact_summary()
        else:
            local_runnable_state_desc = "No runnable state data"
        
        # Step 7: 最终结果处理
        frame_info_dur = frame_row['frame_info_dur']
        local_cpu_state_summary_str = local_cpu_state_summary.__str__()
        
        local_logger.debug(f"Frame {frame_row['frame_idx']} ({frame_info_dur/1E6:.2f}ms) classified as: {category}, {local_cpu_state_summary_str}, Block functions: {local_block_function_desc}, Sleep wakers: {local_sleep_waker_desc}")
        
        # 返回所有结果
        result = {
            'frame_index': frame_index,
            'category': category,
            'main_reason': main_reason,
            'cpu_state_summary': local_cpu_state_summary,
            'cpu_state_summary_str': local_cpu_state_summary_str,
            'blocked_function_time_dict': local_block_function_desc,
            'sleep_waker_info': local_sleep_waker_desc,
            'local_block_function': local_block_function,
            'local_sleep_waker': local_sleep_waker,
            'big_core_occupiers': big_core_occupiers_l[0] if len(big_core_occupiers_l) > 0 else [],
            'local_runnable_state_cpu_occupiers': local_runnable_state_cpu_occupiers,
            'local_runnable_state_analysis': local_runnable_state_desc,
            'frame_stages_breakdown': features.get('frame_stages_breakdown', "Not Available"),
            'features': features,  # 返回所有特征以便后续分析
        }
        
        return result

        
    except Exception as e:
        local_logger.exception(f"Error analyzing frame {frame_index}: {e}")
        return {
            'frame_index': frame_index,
            'category': "Analysis Failed",
            'main_reason': "Analysis Failed",
            'cpu_state_summary': CPUStateSummary(),
            'cpu_state_summary_str': "Analysis Failed",
            'blocked_function_time_dict': "Analysis Failed",
            'sleep_waker_info': "Analysis Failed",
            'local_block_function': {},
            'local_sleep_waker': {},
            'big_core_occupiers': [],
            'local_runnable_state_cpu_occupiers': [],
            'local_runnable_state_analysis': "Analysis Failed",
            'frame_stages_breakdown': 'Analysis Failed',
            'features': {}
        }

if __name__ == "__main__":
    logger = setup_logging()
    trace_path = sys.argv[1]
    pid_match = re.search(r'perfetto-(.*)-(\d+)-\d+\.\d+\.trace', os.path.basename(trace_path))
    if pid_match:
        foreground_package = pid_match.group(1)
        main_proc_pid = int(pid_match.group(2))
        logger.debug(f"Extracted PID from trace file name: {main_proc_pid}")
    else:
        raise Exception("Could not extract PID from trace file name")
    # df = process_frame_trace(trace_path, foreground_package, main_proc_pid, debug=True)
    output_csv = Path(trace_path).parent / os.path.basename(trace_path).replace('.trace', '.csv')
    # Save DataFrame to CSV file
    # output_csv = Path(trace_path).parent / os.path.basename(trace_path).replace('.trace', '.csv')
    # df.to_csv(output_csv, index=False)
    # exit(0)
    # logger.info(f"Frame data saved to {output_csv}")
    # frame_ret_csv = glob.glob(str(Path(trace_path).parent / f"perfetto-{foreground_package}*.csv"))
    if os.path.exists(output_csv):
        df = pd.read_csv(output_csv)
    else:
        df = process_frame_trace(trace_path, foreground_package, main_proc_pid, debug=True)
    # print(df)
    df, big_core_occupiers, global_cpu_state_summary, global_blocked_function, global_sleep_waker, global_runnable_state_analysis, global_frame_stages_breakdown \
        = classify_jank(df, trace_path, main_proc_pid, foreground_package, logger)
    # save four results to file for view
    # output_csv = Path(trace_path).parent / f"perfetto-{foreground_package}-{main_proc_pid}.csv"
    df.to_csv(output_csv, index=False)
    def log_and_save(message, file_handle, logger):
        """同时输出到logger和文件"""
        logger.info(message)
        print(message, file=file_handle)

    with open(os.path.join(Path(trace_path).parent, "jank_analysis.txt"), 'w') as f:
        log_and_save(f"Self-jank classification results saved to {output_csv}", f, logger)
        log_and_save(f"Global Frame Stages Breakdown: {global_frame_stages_breakdown}", f, logger)
        log_and_save(f"Big Core Occupiers: {big_core_occupiers}", f, logger)
        log_and_save(f"Global CPU State Summary: {global_cpu_state_summary}", f, logger)
        log_and_save(f"Global Blocked Function Durations: {describe_blocked_functions(global_blocked_function)}", f, logger)
        log_and_save(f"Global Sleep Waker Info: {describe_sleep_waker_info(global_sleep_waker)}", f, logger)
        log_and_save(f"Global Runnable State Analysis:", f, logger)
        if global_runnable_state_analysis:
            global_analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(global_runnable_state_analysis)
            global_analyzer.run_analysis()
            log_and_save(global_analyzer.get_global_summary(), f, logger)
        else:
            log_and_save("No global runnable state data available", f, logger)
    # mp, rp = get_main_render_avg_priority(df, trace_path, None, "com.instagram.android", logger, True)
    # print(big_core_occupiers)
    # print(mp)
    # print(rp)



