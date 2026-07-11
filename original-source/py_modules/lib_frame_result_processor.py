import glob
import os
import json
from pathlib import Path
import traceback
from typing import OrderedDict, Optional
from datetime import datetime
import pandas as pd
from py_modules.lib_sched_analyzer import MultiWindowSchedSliceAnalyzer
from py_modules.logging_lib import MyLogger, setup_logging
from py_modules.lib_frame_processor_inner import process_frame_trace
from py_modules.lib_jank_classifier import classify_jank, describe_blocked_functions, describe_sleep_waker_info
import subprocess
class ExperimentResultProcessor:
    """处理实验结果的类，从FrameProcessorServer._handle_stop中拆分出来"""
    
    def __init__(
        self, 
        trace_path: str, 
        report_folder: str, 
        logger: MyLogger, 
        foreground_package: str, 
        foreground_pid: Optional[int] = None,
        background_package: Optional[str] = None, 
        background_pid: Optional[int] = None, 
        use_memory: bool = False,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        gpu_results: Optional[dict] = None
    ):
        """
        初始化实验结果处理器
        
        Args:
            trace_path: Perfetto trace文件路径
            report_folder: 结果保存目录
            logger: 日志记录器
            foreground_package: 前台应用包名
            foreground_pid: 前台应用PID
            background_package: 后台应用包名
            background_pid: 后台应用PID
            use_memory: 是否使用内存优化
            start_time: 实验开始时间
            end_time: 实验结束时间
            gpu_results: GPU指标结果
        """
        self.trace_path = trace_path
        self.report_folder = report_folder
        self.logger = logger
        self.foreground_package = foreground_package
        self.foreground_pid = foreground_pid
        self.background_package = background_package
        self.background_pid = background_pid
        self.use_memory = use_memory
        self.start_time = start_time
        self.end_time = end_time
        self.gpu_results = gpu_results
    
    def process(self) -> bool:
        """
        处理实验结果
        
        Returns:
            bool: 处理是否成功
        """
        try:
            # 处理前台应用帧数据
            success_fg = self.process_foreground_app()
            if not success_fg:
                return False
            
            # 处理后台应用帧数据（如果有）
            if self.background_package and self.background_package != "":
                success_bg = self.process_background_app()
                if not success_bg:
                    self.logger.warning("Failed to process background app data")
            
            # 处理GPU指标（如果有）
            if self.gpu_results:
                success_gpu = self.process_gpu_metrics()
                if not success_gpu:
                    self.logger.warning("Failed to process GPU metrics")
            
            return True
        except Exception as e:
            self.logger.error(f"Error processing experiment results: {e}")
            self.logger.error(traceback.format_exc())
            return False
    
    def process_foreground_app(self) -> bool:
        """
        处理前台应用的帧数据
        
        Returns:
            bool: 处理是否成功
        """
        try:
            self.logger.debug(f"Processing foreground app trace: {self.trace_path}")
            # 调用外部脚本生成CPU profile JSON
            try:
                cmd = [
                    "python",
                    (Path(__file__).parent / "generate_task_profile_json.py").as_posix(),
                    "--trace_path", self.trace_path
                ]
                if self.foreground_package:
                    cmd.extend(["--pkg_name", self.foreground_package])
                subprocess.run(cmd, check=True)
            except Exception as e:
                self.logger.warning(f"Failed to generate CPU profile JSON: {e}")
            
            # 处理帧数据
            ret: dict[any, any] = OrderedDict()

            frame_ret_csv = None
            if os.path.exists(self.trace_path.replace('.trace', '.csv')):
                frame_ret_csv = [self.trace_path.replace('.trace', '.csv')]
            if frame_ret_csv is None:
                frame_ret_csv = glob.glob(str(Path(self.trace_path).parent / f"perfetto-{self.foreground_package}*.csv"))
            if True: # len(frame_ret_csv) != 1:
                self.logger.debug(f"Processing foreground app trace: {self.foreground_package}")
                df = process_frame_trace(
                    self.trace_path, 
                    self.foreground_package, 
                    self.foreground_pid, 
                    None, 
                    result_dict=ret
                )

                # 添加时间信息
                if self.start_time and self.end_time:
                    duration_sec = (self.end_time - self.start_time).total_seconds()
                    ret['start_time'] = self.start_time.strftime("%Y-%m-%d-%H:%M:%S")
                    ret['end_time'] = self.end_time.strftime("%Y-%m-%d-%H:%M:%S")
                    ret['duration_sec'] = duration_sec
                    ret['fg_fps_all_time'] = len(df) / duration_sec if duration_sec > 0 else 0
                    self.logger.debug(f"Experiment duration: {duration_sec:.2f} seconds")
                    self.logger.debug(f"fg_fps_all_time: {len(df) / duration_sec:.2f} fps")
                info_level_logger = setup_logging(console_level="INFO")
                info_level_logger.setLevel("INFO")
                df, big_core_occupiers, global_cpu_state_summary, global_blocked_function, global_sleep_waker, global_runnable_state_analysis, global_frame_stages_breakdown \
                    = classify_jank(df, self.trace_path, self.foreground_pid, self.foreground_package, info_level_logger)
                output_csv = os.path.join(self.report_folder, f"perfetto-{self.foreground_package}-{self.foreground_pid}.csv") if len(frame_ret_csv) == 0 else frame_ret_csv[0]
                df.to_csv(output_csv, index=False)
                def log_and_save(message, file_handle, logger):
                    """同时输出到logger和文件"""
                    logger.info(message)
                    print(message, file=file_handle)

                with open(os.path.join(self.report_folder, "jank_analysis.txt"), 'w') as f:
                    log_and_save(f"Self-jank classification results saved to {output_csv}", f, self.logger)
                    log_and_save(f"Global Frame Stages Breakdown: {global_frame_stages_breakdown}", f, self.logger)
                    log_and_save(f"Big Core Occupiers: {big_core_occupiers}", f, self.logger)
                    log_and_save(f"Global CPU State Summary: {global_cpu_state_summary}", f, self.logger)
                    log_and_save(f"Global Blocked Function Durations: {describe_blocked_functions(global_blocked_function)}", f, self.logger)
                    log_and_save(f"Global Sleep Waker Info: {describe_sleep_waker_info(global_sleep_waker)}", f, self.logger)
                    log_and_save(f"Global Runnable State Analysis:", f, self.logger)
                    if global_runnable_state_analysis:
                        global_analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(global_runnable_state_analysis)
                        global_analyzer.run_analysis()
                        log_and_save(global_analyzer.get_global_summary(), f, self.logger)
                    else:
                        log_and_save("No global runnable state data available", f, self.logger)
                # big_core_occupiers = list[(process, thread, run_ms)], convert to pd
                big_core_occupiers_df = pd.DataFrame(big_core_occupiers, columns=['process', 'thread', 'run_ms'])
                big_core_occupiers_csv = os.path.join(self.report_folder, "big_core_occupiers.csv")
                big_core_occupiers_df.to_csv(big_core_occupiers_csv, index=False)
            else:
                self.logger.debug(f"Reusing existing CSV for foreground app trace: {frame_ret_csv[0]}")
                df = pd.read_csv(frame_ret_csv[0])

            self.logger.debug(f"Calculating foreground app thread priority")
            # mp, rp = get_main_render_avg_priority(df, self.trace_path, self.foreground_pid, self.foreground_package, self.logger)
            # save mp, rp to file 
            # with open(os.path.join(self.report_folder, "priority.csv"), 'w') as f:
            #     f.write(f"fg_main_thread_priority,{mp}\n")
            #     f.write(f"fg_render_thread_priority,{rp}\n")
                
            self.logger.debug(f"Processed foreground app frames: {len(df)} frames")
            
            
            # 添加GPU使用信息（如果有）
            if self.gpu_results and 'gpu_usage_avg' in self.gpu_results:
                ret['gpu_usage_avg'] = self.gpu_results['gpu_usage_avg']
            
            # 保存结果
            df_result = pd.DataFrame([ret])
            
            # 保存为CSV格式
            with open(os.path.join(self.report_folder, "ms_ret.csv"), 'w') as f:
                for key, value in ret.items():
                    f.write(f"{key},{value}\n")
            
            df_result.to_csv(os.path.join(self.report_folder, "ms_ret_df.csv"), index=False)
            
            # 保存帧详细数据
            decoded_csv = os.path.basename(self.trace_path).replace('.trace', '.csv')
            decoded_csv = os.path.join(self.report_folder, decoded_csv)
            df.to_csv(decoded_csv, index=False)
            
            # 保存帧统计结果
            if len(df) == 0:
                ret = [self.foreground_package, 0, 0, 0, 0, 0]
            else:
                # 计算空闲时间
                idle_time_ns = 0
                for i in range(len(df) - 2):
                    interval = df['ts'][i + 1] - (df['ts'][i] + df['rendering_time'][i])
                    if interval > 1e8:  # 100ms
                        idle_time_ns += interval

                total_time_ns = df['ts'].iloc[-1] + df['rendering_time'].iloc[-1] - df['ts'].iloc[0]

                frame_cnt = len(df)
                frame_drop = len(df[df['is_drop'] == True])
                valid_sec = round((total_time_ns - idle_time_ns) / 1e9, 2)
                fps = len(df) / valid_sec
                ria = len(df[df['rendering_time'] > 16666666])

                ret = [self.foreground_package, frame_cnt, frame_drop, valid_sec, fps, ria]
            
            frame_df = pd.DataFrame([ret], columns=['comm', 'frame_cnt', 'frame_drop', 'valid_sec', 'fps', 'ria'])
            result_fn = decoded_csv.replace("perfetto-", "frame_ret-")
            frame_df.to_csv(result_fn, index=False)
            self.logger.debug(f"Frame processing result saved to {result_fn}")
            
            return True
        except Exception as e:
            self.logger.error(f"Error processing foreground app data: {e}")
            self.logger.error(traceback.format_exc())
            return False
    
    def process_background_app(self) -> bool:
        """
        处理后台应用的帧数据
        
        Returns:
            bool: 处理是否成功
        """
        try:
            self.logger.debug(f"Processing background app trace: {self.background_package}")
            
            # 处理后台应用帧数据
            ret_bg = OrderedDict()

            frame_ret_csv = glob.glob(str(Path(self.trace_path).parent / f"perfetto-{self.background_package}*.csv"))
            if len(frame_ret_csv) != 1:
                self.logger.debug(f"Processing background app trace: {self.background_package}")
                df_bg = process_frame_trace(
                    self.trace_path, 
                    self.background_package, 
                    self.background_pid, 
                    result_dict=ret_bg
                )
            else:
                self.logger.debug(f"Reusing existing CSV for background app trace: {frame_ret_csv[0]}")
                df_bg = pd.read_csv(frame_ret_csv[0])

            self.logger.debug(f"Calculating background app thread priority")
            # mp, rp = get_main_render_avg_priority(df_bg, self.trace_path, None, self.background_package, self.logger, is_bg=True)
            # with open(os.path.join(self.report_folder, "priority.csv"), 'a') as f:
            #     f.write(f"bg_main_thread_priority,{mp}\n")
            #     f.write(f"bg_render_thread_priority,{rp}\n")
            
            # 保存结果
            with open(os.path.join(self.report_folder, "bg_ret.csv"), 'w') as f:
                for key, value in ret_bg.items():
                    f.write(f"{key},{value}\n")
            
            # 保存帧详细数据
            memory_flag = "memory" if self.use_memory else "query_llm"
            postfix = os.path.basename(self.trace_path).split('-')[-1].replace('.trace', '')
            bg_csv = os.path.join(self.report_folder, 
                               f"perfetto-{self.background_package}_{memory_flag}-{self.background_pid if self.background_pid else 'unknown'}-{postfix}.csv")
            df_bg.to_csv(bg_csv, index=False)
            self.logger.debug(f"Background app frames csv saved to {bg_csv}")
            
            return True
        except Exception as e:
            self.logger.error(f"Error processing background app data: {e}")
            self.logger.error(traceback.format_exc())
            return False
    
    def process_gpu_metrics(self) -> bool:
        """
        处理GPU指标数据
        
        Returns:
            bool: 处理是否成功
        """
        try:
            if not self.gpu_results:
                return False
            
            # 保存GPU使用结果
            gpu_results_file = os.path.join(self.report_folder, "gpu_usage.json")
            with open(gpu_results_file, 'w') as f:
                json.dump(self.gpu_results, f, indent=2)
            
            self.logger.debug(f"GPU usage results saved to {gpu_results_file}, average usage: {self.gpu_results['gpu_usage_avg']:.4f}")
            self.logger.debug(f"GPU raw data saved: {len(self.gpu_results['gpu_usage_raw_output'])} samples")
            
            # 保存详细样本数据到CSV
            gpu_samples_file = os.path.join(self.report_folder, "gpu_samples.csv")
            with open(gpu_samples_file, 'w') as f:
                f.write("sample_id,busy,elapsed,utilization\n")
                for i, (busy, elapsed) in enumerate(zip(self.gpu_results['gpu_busy_list'], self.gpu_results['gpu_elapsed_list'])):
                    util = busy / elapsed if elapsed > 0 else 0
                    f.write(f"{i+1},{busy},{elapsed},{util:.6f}\n")
            
            self.logger.debug(f"GPU detailed sample data saved to {gpu_samples_file}")
            
            # 保存原始输出到文本文件
            gpu_raw_file = os.path.join(self.report_folder, "gpu_usage_raw.txt")
            with open(gpu_raw_file, 'w') as f:
                for line in self.gpu_results['gpu_usage_raw_output']:
                    f.write(f"{line}\n")
            
            return True
        except Exception as e:
            self.logger.error(f"Error processing GPU metrics: {e}")
            self.logger.error(traceback.format_exc())
            return False