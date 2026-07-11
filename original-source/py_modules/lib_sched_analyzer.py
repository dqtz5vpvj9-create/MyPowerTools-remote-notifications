# %%
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from typing import List, Tuple, Optional, Dict

def query_sched_slices(
        query_func,
        analysis_start: int,
        analysis_end: int
) -> pd.DataFrame:
    """
    Query sched slices within a specified time range.
    
    Args:
        analysis_start: Start timestamp for analysis
        analysis_end: End timestamp for analysis
        
    Returns:
        DataFrame containing sched slices with columns:
        'id', 'ts', 'dur', 'cpu', 'utid', 'tid', 'upid', 'pid', 'is_main_thread', 'thread_name', 'pid_name'
    """
    df = query_func(f"""
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
    WHERE s.ts + s.dur > {analysis_start} AND s.ts < {analysis_end} AND s.utid != 0
    ORDER BY s.ts
    """, "sched_slices_query")
    # 修正跨界持续时间
    df['overlap_start'] = df['ts'].clip(lower=analysis_start)
    df['overlap_end']   = (df['ts'] + df['dur']).clip(upper=analysis_end)
    df['overlap_dur']   = (df['overlap_end'] - df['overlap_start']).clip(lower=0)
    return df

class MultiWindowSchedSliceAnalyzer:
    """
    Multi-window scheduler slice analyzer for jank frame analysis.
    
    This class provides comprehensive CPU usage analysis across multiple time windows,
    perfect for analyzing hundreds of jank frames and computing average statistics.
    
    
    Features:
    - Direct DataFrame input (recommended) or file-based input (legacy)
    - Multiple time windows analysis across different datasets
    - Global statistics aggregation across all jank frames
    - Per-window and aggregated CPU usage analysis
    - Big vs Small CPU comparison across all frames
    - Process and thread level analysis with global ranking
    - Comprehensive visualization capabilities
    
    Recommended Usage:
        jank_data = [(df1, start1, end1), (df2, start2, end2), ...]
        analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(jank_data)
        analyzer.run_analysis()
        analyzer.print_global_summary()
    """
    
    def __init__(self):
        """Initialize the multi-window analyzer."""
        self.windows = []  # List of window dictionaries
        self.dataframes = []  # List of cached dataframes corresponding to windows
        self.big_cpu_cores = [6, 7]
        self.small_cpu_cores = [0, 1, 2, 3, 4, 5]
        
        # Analysis results
        self.window_analyses = []  # Individual window analysis results
        self.global_stats = None
        self.aggregated_thread_usage = None
        self.aggregated_process_usage = None
        self.aggregated_cpu_usage = None
        self.window_comparison = None
        
    def add_dataframe_window(self, df: pd.DataFrame, start_time: int, end_time: int, label: Optional[str] = None):
        """
        Add a dataframe with time window for analysis.
        
        Args:
            df (pd.DataFrame): The DataFrame containing sched slice data
            start_time (int): Start timestamp for analysis window
            end_time (int): End timestamp for analysis window
            label (str): Optional label for this window
        """
        window_id = len(self.windows)
        if label is None:
            label = f"Window_{window_id}"
        
        window = {
            'id': window_id,
            'start_time': start_time,
            'end_time': end_time,
            'label': label,
            'duration_ns': end_time - start_time
        }
        
        self.windows.append(window)
        df = df[
            (df['ts'] < window['end_time']) & 
            (df['ts'] + df['dur'] > window['start_time'])
        ]
        self.dataframes.append(df.copy())  # Store a copy to avoid side effects

    def add_time_window(self, data_file: str, start_time: int, end_time: int, label: Optional[str] = None):
        """
        Add a time window for analysis (legacy method for file-based input).
        
        Args:
            data_file (str): Path to the TSV/CSV data file
            start_time (int): Start timestamp for analysis window
            end_time (int): End timestamp for analysis window
            label (str): Optional label for this window
        """
        # Load dataframe from file
        if 'tsv' in data_file:
            df = pd.read_table(data_file)
        elif 'csv' in data_file:
            df = pd.read_csv(data_file)
        else:
            raise ValueError(f"Unsupported file format: {data_file}")
        
        # Use the new dataframe-based method
        self.add_dataframe_window(df, start_time, end_time, label)
    
    def add_data_windows(self, data_windows: List[Tuple[pd.DataFrame, int, int]], labels: Optional[List[str]] = None):
        """
        Add multiple dataframes with their respective time windows.
        
        Args:
            data_windows: List of (dataframe, start_time, end_time) tuples
            labels: Optional list of labels for each window
        """
        for i, (df, start_time, end_time) in enumerate(data_windows):
            label = labels[i] if labels and i < len(labels) else f"DataFrame_Window_{i}"
            self.add_dataframe_window(df, start_time, end_time, label)

    def add_jank_frames(self, jank_frames: List[Tuple[str, int, int]]):
        """
        Add multiple jank frames at once (legacy method for file-based input).
        
        Args:
            jank_frames: List of (data_file, start_time, end_time) tuples
        """
        for i, (data_file, start_time, end_time) in enumerate(jank_frames):
            self.add_time_window(data_file, start_time, end_time, f"Jank_{i+1}")

    def add_jank_dataframes(self, jank_dataframes: List[Tuple[pd.DataFrame, int, int]], labels: Optional[List[str]] = None):
        """
        Add multiple jank frames as dataframes.
        
        Args:
            jank_dataframes: List of (dataframe, start_time, end_time) tuples
            labels: Optional list of labels for each jank frame
        """
        for i, (df, start_time, end_time) in enumerate(jank_dataframes):
            label = labels[i] if labels and i < len(labels) else f"Jank_{i+1}"
            self.add_dataframe_window(df, start_time, end_time, label)
    
    def _analyze_single_window(self, window, df):
        """Analyze a single time window."""
        # Make a copy to avoid modifying the original
        # Filter dataframe to only include slices that overlap with the time window
        
        # Calculate overlap durations within the time window
        df['overlap_start'] = df['ts'].clip(lower=window['start_time'])
        df['overlap_end'] = (df['ts'] + df['dur']).clip(upper=window['end_time'])
        df['overlap_dur'] = (df['overlap_end'] - df['overlap_start']).clip(lower=0)
        
        total_duration_ns = window['end_time'] - window['start_time']
        total_usage = df['overlap_dur'].sum()
        
        # Per-CPU analysis
        per_cpu_usage = (
            df.groupby('cpu', as_index=False)['overlap_dur']
            .sum()
            .rename(columns={'overlap_dur': 'cpu_usage_ns'})
            .assign(
                cpu_usage_ms=lambda x: x.cpu_usage_ns/1e6,
                utilization_percent=lambda x: (x.cpu_usage_ns / total_duration_ns) * 100
            )
            .sort_values('cpu')
        )
        
        # CPU type analysis
        big_cpu_usage = per_cpu_usage[per_cpu_usage['cpu'].isin(self.big_cpu_cores)]['cpu_usage_ns'].sum()
        small_cpu_usage = per_cpu_usage[per_cpu_usage['cpu'].isin(self.small_cpu_cores)]['cpu_usage_ns'].sum()
        
        big_cpu_utilization = (big_cpu_usage / (total_duration_ns * len(self.big_cpu_cores))) * 100
        small_cpu_utilization = (small_cpu_usage / (total_duration_ns * len(self.small_cpu_cores))) * 100
        
        # Thread analysis
        thread_usage = (
            df.groupby(['pid_name', 'thread_name'], as_index=False)['overlap_dur']
            .sum()
            .assign(
                usage_ms=lambda x: x.overlap_dur/1e6,
                percentage=lambda x: x.overlap_dur/total_usage*100 if total_usage > 0 else 0,
                identifier=lambda x: x.pid_name + "/" + x.thread_name
            )
            .sort_values('overlap_dur', ascending=False)
        )
        
        # Process analysis
        process_usage = (
            df.groupby('pid_name', as_index=False)['overlap_dur']
            .sum()
            .assign(
                usage_ms=lambda x: x.overlap_dur/1e6,
                percentage=lambda x: x.overlap_dur/total_usage*100 if total_usage > 0 else 0
            )
            .sort_values('overlap_dur', ascending=False)
        )
        
        return {
            'window': window,
            'total_duration_ns': total_duration_ns,
            'total_usage_ns': total_usage,
            'avg_cpu_utilization': (total_usage / (total_duration_ns * 8)) * 100,
            'big_cpu_utilization': big_cpu_utilization,
            'small_cpu_utilization': small_cpu_utilization,
            'per_cpu_usage': per_cpu_usage,
            'thread_usage': thread_usage,
            'process_usage': process_usage
        }
    
    def run_analysis(self):
        """Run analysis on all time windows."""
        if not self.windows:
            raise ValueError("No time windows added. Use add_time_window() first.")
        
        # print(f"Analyzing {len(self.windows)} time windows...")
        
        # Analyze each window
        self.window_analyses = []
        for i, window in enumerate(self.windows):
            df = self.dataframes[i]  # Get corresponding dataframe
            analysis = self._analyze_single_window(window, df)
            self.window_analyses.append(analysis)
        
        # Compute global statistics
        self._compute_global_statistics()
        self._aggregate_thread_usage()
        self._aggregate_process_usage()
        self._aggregate_cpu_usage()
        self._create_window_comparison()
        
        # print(f"Analysis complete!")
    
    def _compute_global_statistics(self):
        """Compute global statistics across all windows."""
        total_windows = len(self.window_analyses)
        total_time_ms = sum(a['total_duration_ns'] for a in self.window_analyses) / 1e6
        
        avg_cpu_util = np.mean([a['avg_cpu_utilization'] for a in self.window_analyses])
        avg_big_cpu_util = np.mean([a['big_cpu_utilization'] for a in self.window_analyses])
        avg_small_cpu_util = np.mean([a['small_cpu_utilization'] for a in self.window_analyses])
        
        # Identify bottleneck pattern
        big_cpu_dominant = sum(1 for a in self.window_analyses if a['big_cpu_utilization'] > a['small_cpu_utilization'])
        small_cpu_dominant = total_windows - big_cpu_dominant
        balanced = sum(1 for a in self.window_analyses if abs(a['big_cpu_utilization'] - a['small_cpu_utilization']) < 5)
        
        if big_cpu_dominant > small_cpu_dominant:
            bottleneck = "Big CPU Cores"
        elif small_cpu_dominant > big_cpu_dominant:
            bottleneck = "Small CPU Cores"
        else:
            bottleneck = "Balanced"
        
        self.global_stats = {
            'total_windows': total_windows,
            'total_time_ms': total_time_ms,
            'avg_cpu_utilization': avg_cpu_util,
            'avg_big_cpu_utilization': avg_big_cpu_util,
            'avg_small_cpu_utilization': avg_small_cpu_util,
            'big_cpu_dominant_windows': big_cpu_dominant,
            'small_cpu_dominant_windows': small_cpu_dominant,
            'balanced_windows': balanced,
            'bottleneck_pattern': bottleneck,
            'avg_window_duration_ms': total_time_ms / total_windows
        }
    
    def _aggregate_thread_usage(self):
        """Aggregate thread usage across all windows."""
        all_threads = []
        
        for analysis in self.window_analyses:
            threads = analysis['thread_usage'].copy()
            threads['window_id'] = analysis['window']['id']
            threads['window_label'] = analysis['window']['label']
            all_threads.append(threads)
        
        combined_threads = pd.concat(all_threads, ignore_index=True)
        
        # Aggregate by thread identifier
        self.aggregated_thread_usage = (
            combined_threads.groupby('identifier', as_index=False)
            .agg({
                'overlap_dur': 'sum',
                'usage_ms': 'sum',
                'window_id': 'count'  # Number of windows this thread appeared in
            })
            .rename(columns={'window_id': 'appearance_count'})
            .assign(
                avg_usage_ms_per_appearance=lambda x: x.usage_ms / x.appearance_count,
                total_percentage=lambda x: x.overlap_dur / sum(a['total_usage_ns'] for a in self.window_analyses) * 100
            )
            .sort_values('overlap_dur', ascending=False)
        )
        
        # Add cumulative percentage
        self.aggregated_thread_usage['cumulative_percentage'] = self.aggregated_thread_usage['total_percentage'].cumsum()
    
    def _aggregate_process_usage(self):
        """Aggregate process usage across all windows."""
        all_processes = []
        
        for analysis in self.window_analyses:
            processes = analysis['process_usage'].copy()
            processes['window_id'] = analysis['window']['id']
            all_processes.append(processes)
        
        combined_processes = pd.concat(all_processes, ignore_index=True)
        
        # Aggregate by process name
        self.aggregated_process_usage = (
            combined_processes.groupby('pid_name', as_index=False)
            .agg({
                'overlap_dur': 'sum',
                'usage_ms': 'sum',
                'window_id': 'count'
            })
            .rename(columns={'window_id': 'appearance_count'})
            .assign(
                avg_usage_ms_per_appearance=lambda x: x.usage_ms / x.appearance_count,
                total_percentage=lambda x: x.overlap_dur / sum(a['total_usage_ns'] for a in self.window_analyses) * 100
            )
            .sort_values('overlap_dur', ascending=False)
        )
        
        # Add cumulative percentage
        self.aggregated_process_usage['cumulative_percentage'] = self.aggregated_process_usage['total_percentage'].cumsum()
    
    def _aggregate_cpu_usage(self):
        """Aggregate CPU usage across all windows."""
        all_cpu_usage = []
        
        for analysis in self.window_analyses:
            cpu_usage = analysis['per_cpu_usage'].copy()
            cpu_usage['window_id'] = analysis['window']['id']
            cpu_usage['window_duration_ns'] = analysis['total_duration_ns']
            all_cpu_usage.append(cpu_usage)
        
        combined_cpu = pd.concat(all_cpu_usage, ignore_index=True)
        
        # Calculate average utilization per CPU across all windows
        self.aggregated_cpu_usage = (
            combined_cpu.groupby('cpu', as_index=False)
            .agg({
                'cpu_usage_ns': 'sum',
                'utilization_percent': 'mean',  # Average utilization
                'window_id': 'count'
            })
            .rename(columns={'window_id': 'total_windows', 'utilization_percent': 'avg_utilization_percent'})
            .assign(
                total_usage_ms=lambda x: x.cpu_usage_ns / 1e6,
                cpu_type=lambda x: x.cpu.apply(lambda core: 'Big' if core in self.big_cpu_cores else 'Small')
            )
            .sort_values('cpu')
        )
    
    def _create_window_comparison(self):
        """Create comparison table across all windows."""
        comparison_data = []
        
        for analysis in self.window_analyses:
            window = analysis['window']
            comparison_data.append({
                'window_id': window['id'],
                'label': window['label'],
                'duration_ms': analysis['total_duration_ns'] / 1e6,
                'avg_cpu_util': analysis['avg_cpu_utilization'],
                'big_cpu_util': analysis['big_cpu_utilization'],
                'small_cpu_util': analysis['small_cpu_utilization'],
                'total_cpu_time_ms': analysis['total_usage_ns'] / 1e6,
                'top_thread': analysis['thread_usage'].iloc[0]['identifier'] if not analysis['thread_usage'].empty else 'N/A',
                'top_thread_pct': analysis['thread_usage'].iloc[0]['percentage'] if not analysis['thread_usage'].empty else 0,
                'top_process': analysis['process_usage'].iloc[0]['pid_name'] if not analysis['process_usage'].empty else 'N/A',
                'top_process_pct': analysis['process_usage'].iloc[0]['percentage'] if not analysis['process_usage'].empty else 0
            })
        
        self.window_comparison = pd.DataFrame(comparison_data)
    
    def get_global_summary(self) -> str:
        """Return comprehensive global summary as a string."""
        if self.global_stats is None:
            self.run_analysis()
        
        lines = []
        lines.append("=" * 80)
        lines.append("MULTI-WINDOW JANK ANALYSIS SUMMARY")
        lines.append("=" * 80)
        
        lines.append(f"\n=== Global Statistics ===")
        lines.append(f"Total time windows analyzed: {self.global_stats['total_windows']}")
        lines.append(f"Total wall time: {self.global_stats['total_time_ms']:.2f} ms")
        lines.append(f"Average time window duration: {self.global_stats['avg_window_duration_ms']:.2f} ms")
        lines.append(f"Average CPU utilization: {self.global_stats['avg_cpu_utilization']:.2f}%")
        lines.append(f"Average Big CPU utilization: {self.global_stats['avg_big_cpu_utilization']:.2f}%")
        lines.append(f"Average Small CPU utilization: {self.global_stats['avg_small_cpu_utilization']:.2f}%")
        # lines.append(f"Bottleneck pattern: {self.global_stats['bottleneck_pattern']}")
        # lines.append(f"Big CPU dominant : {self.global_stats['big_cpu_dominant_windows']}")
        # lines.append(f"Small CPU dominant frames: {self.global_stats['small_cpu_dominant_windows']}")
        # lines.append(f"Balanced frames: {self.global_stats['balanced_windows']}")
        
        lines.append(f"\n=== Aggregated CPU Usage ===")
        lines.append(self.aggregated_cpu_usage[['cpu', 'cpu_type', 'total_usage_ms', 'avg_utilization_percent']].to_string(index=False))
        
        lines.append(f"\n=== Top Problematic Threads (Across All Frames) ===")
        top_threads = self.aggregated_thread_usage.head(15)
        lines.append(top_threads[['identifier', 'usage_ms', 'total_percentage', 'appearance_count', 'avg_usage_ms_per_appearance']].to_string(index=False))
        
        lines.append(f"\n=== Top Problematic Processes (Across All Frames) ===")
        top_processes = self.aggregated_process_usage.head(10)
        lines.append(top_processes[['pid_name', 'usage_ms', 'total_percentage', 'appearance_count', 'avg_usage_ms_per_appearance']].to_string(index=False))
        
        # Find threads that appear in most frames
        frequent_threads = self.aggregated_thread_usage[
            self.aggregated_thread_usage['appearance_count'] >= self.global_stats['total_windows'] * 0.5
        ].head(10)
        
        if not frequent_threads.empty:
            lines.append(f"\n=== Most Frequent Problem Threads (>50% of frames) ===")
            lines.append(frequent_threads[['identifier', 'usage_ms', 'appearance_count', 'avg_usage_ms_per_appearance']].to_string(index=False))
        
        return "\n".join(lines)
    def get_compact_summary(self):
        """Print extremely compact one-line summary with most important info."""
        if self.global_stats is None:
            self.run_analysis()
        
        # Get top 15 threads
        top_threads = self.aggregated_thread_usage.head(15)
        
        # Format: "CPU: xx%, Process/Thread: xx%"
        cpu_util = f"CPU: {self.global_stats['avg_cpu_utilization']:.1f}%"
        
        thread_info = []
        for _, thread in top_threads.iterrows():
            thread_info.append(f"{thread['identifier']}: {thread['total_percentage']:.1f}%")
        
        return f"{cpu_util}; {'; '.join(thread_info)}"
    
    def print_window_comparison(self):
        """Print detailed comparison across windows."""
        if self.window_comparison is None:
            self.run_analysis()
        
        print(f"\n=== Individual Window Comparison ===")
        print(self.window_comparison[['label', 'duration_ms', 'avg_cpu_util', 'big_cpu_util', 'small_cpu_util']].to_string(index=False))
        
        # Find worst performing windows
        worst_window = self.window_comparison.loc[self.window_comparison['avg_cpu_util'].idxmax()]
        print(f"\nWorst performing window: {worst_window['label']} ({worst_window['avg_cpu_util']:.1f}% CPU utilization)")
        print(f"Top culprit: {worst_window['top_thread']} ({worst_window['top_thread_pct']:.1f}%)")
    
    def visualize_global_analysis(self, figsize=(20, 16)):
        """Create comprehensive visualization of global analysis."""
        if self.global_stats is None:
            self.run_analysis()
        
        fig, ((ax1, ax2), (ax3, ax4), (ax5, ax6)) = plt.subplots(3, 2, figsize=figsize)
        
        # 1. Average CPU utilization per core
        ax1.bar(self.aggregated_cpu_usage['cpu'], self.aggregated_cpu_usage['avg_utilization_percent'],
                color=['red' if cpu in self.big_cpu_cores else 'blue' for cpu in self.aggregated_cpu_usage['cpu']])
        ax1.set_xlabel('CPU Core')
        ax1.set_ylabel('Average Utilization (%)')
        ax1.set_title('Average CPU Utilization per Core (All Frames)')
        ax1.set_xticks(self.aggregated_cpu_usage['cpu'])
        ax1.legend(['Big CPU', 'Small CPU'])
        
        # 2. Big vs Small CPU distribution
        big_total = self.aggregated_cpu_usage[self.aggregated_cpu_usage['cpu_type'] == 'Big']['total_usage_ms'].sum()
        small_total = self.aggregated_cpu_usage[self.aggregated_cpu_usage['cpu_type'] == 'Small']['total_usage_ms'].sum()
        ax2.pie([big_total, small_total], labels=['Big CPU', 'Small CPU'], autopct='%1.1f%%', startangle=90)
        ax2.set_title('Total CPU Usage Distribution (All Frames)')
        
        # 3. Top problematic processes
        top_processes = self.aggregated_process_usage.head(15)
        ax3.barh(range(len(top_processes)), top_processes['total_percentage'])
        ax3.set_yticks(range(len(top_processes)))
        ax3.set_yticklabels(top_processes['pid_name'], fontsize=8)
        ax3.set_xlabel('Total CPU Usage Percentage (%)')
        ax3.set_title('Top 15 Problematic Processes (All Frames)')
        ax3.invert_yaxis()
        
        # 4. Top problematic threads
        top_threads = self.aggregated_thread_usage.head(15)
        ax4.barh(range(len(top_threads)), top_threads['total_percentage'])
        ax4.set_yticks(range(len(top_threads)))
        ax4.set_yticklabels(top_threads['identifier'], fontsize=6)
        ax4.set_xlabel('Total CPU Usage Percentage (%)')
        ax4.set_title('Top 15 Problematic Threads (All Frames)')
        ax4.invert_yaxis()
        
        # 5. CPU utilization distribution across frames
        utilizations = [a['avg_cpu_utilization'] for a in self.window_analyses]
        ax5.hist(utilizations, bins=20, edgecolor='black', alpha=0.7)
        ax5.axvline(np.mean(utilizations), color='red', linestyle='--', label=f'Mean: {np.mean(utilizations):.1f}%')
        ax5.set_xlabel('CPU Utilization (%)')
        ax5.set_ylabel('Number of Frames')
        ax5.set_title('CPU Utilization Distribution Across Frames')
        ax5.legend()
        
        # 6. Big vs Small CPU utilization scatter
        big_utils = [a['big_cpu_utilization'] for a in self.window_analyses]
        small_utils = [a['small_cpu_utilization'] for a in self.window_analyses]
        ax6.scatter(small_utils, big_utils, alpha=0.6)
        ax6.plot([0, 100], [0, 100], 'r--', label='Equal utilization')
        ax6.set_xlabel('Small CPU Utilization (%)')
        ax6.set_ylabel('Big CPU Utilization (%)')
        ax6.set_title('Big vs Small CPU Utilization (Per Frame)')
        ax6.legend()
        
        plt.tight_layout()
        plt.show()
        
        # Print key insights
        print(f"\n=== Key Insights ===")
        print(f"Average jank severity: {np.mean(utilizations):.1f}% CPU utilization")
        print(f"Worst jank frame: {max(utilizations):.1f}% CPU utilization")
        print(f"Most consistent problem: {self.aggregated_thread_usage.iloc[0]['identifier']}")
        print(f"  - Appears in {self.aggregated_thread_usage.iloc[0]['appearance_count']}/{self.global_stats['total_windows']} frames")
        print(f"  - Average {self.aggregated_thread_usage.iloc[0]['avg_usage_ms_per_appearance']:.2f}ms per appearance")
    
    def get_jank_severity_analysis(self):
        """Analyze jank severity patterns."""
        if self.global_stats is None:
            self.run_analysis()
            
        utilizations = [a['avg_cpu_utilization'] for a in self.window_analyses]
        
        severity_levels = []
        for util in utilizations:
            if util > 80:
                severity = "Severe"
            elif util > 60:
                severity = "High"
            elif util > 40:
                severity = "Medium"
            else:
                severity = "Low"
            severity_levels.append(severity)
        
        severity_counts = pd.Series(severity_levels).value_counts()
        
        return {
            'utilizations': utilizations,
            'severity_levels': severity_levels,
            'severity_distribution': severity_counts.to_dict(),
            'avg_severity': np.mean(utilizations),
            'worst_severity': max(utilizations),
            'p95_severity': np.percentile(utilizations, 95),
            'p99_severity': np.percentile(utilizations, 99)
        }
    
    def export_summary_to_dataframe(self):
        """Export analysis summary to pandas DataFrame for further processing."""
        if self.global_stats is None:
            self.run_analysis()
            
        summary_data = {
            'metric': [],
            'value': []
        }
        
        # Add global stats
        for key, value in self.global_stats.items():
            summary_data['metric'].append(f"global_{key}")
            summary_data['value'].append(value)
        
        # Add severity analysis
        severity = self.get_jank_severity_analysis()
        for key, value in severity.items():
            if key not in ['utilizations', 'severity_levels', 'severity_distribution']:
                summary_data['metric'].append(f"severity_{key}")
                summary_data['value'].append(value)
        
        return pd.DataFrame(summary_data)

    @classmethod
    def from_dataframes(cls, jank_data: List[Tuple[pd.DataFrame, int, int]], labels: Optional[List[str]] = None):
        """
        Create analyzer directly from dataframes - the recommended way.
        
        Args:
            jank_data: List of (dataframe, start_time, end_time) tuples
            labels: Optional list of labels for each jank frame
            
        Returns:
            MultiWindowSchedSliceAnalyzer: Configured analyzer ready for analysis
            
        Example:
            jank_data = [
                (df1, start1, end1),
                (df2, start2, end2),
                (df3, start3, end3)
            ]
            analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(jank_data)
            analyzer.run_analysis()
            analyzer.print_global_summary()
        """
        analyzer = cls()
        analyzer.add_jank_dataframes(jank_data, labels)
        return analyzer

# Legacy class for single window analysis (kept for compatibility)
class SchedSliceAnalyzer:
    """Single window analyzer - legacy compatibility."""
    
    def __init__(self, data_file=None, df=None, start_time=None, end_time=None):
        self.multi_analyzer = MultiWindowSchedSliceAnalyzer()
        if data_file and start_time and end_time:
            self.multi_analyzer.add_time_window(data_file, start_time, end_time, "SingleWindow")
        elif df is not None and start_time is not None and end_time is not None:
            self.multi_analyzer.add_dataframe_window(df, start_time, end_time, "SingleWindow")
    
    def run_full_analysis(self):
        return self.multi_analyzer.run_analysis()
    
    def visualize(self, figsize=(15, 12)):
        return self.multi_analyzer.visualize_global_analysis(figsize)
    
    def print_analysis_results(self):
        return self.multi_analyzer.print_global_summary()
    
    def get_summary_stats(self):
        if self.multi_analyzer.global_stats is None:
            self.multi_analyzer.run_analysis()
        return self.multi_analyzer.global_stats

# %%
# Usage Examples

def example_usage():
    """
    Example usage of the improved MultiWindowSchedSliceAnalyzer.
    """
    # Method 1: Direct dataframe input (RECOMMENDED)
    # Assuming you have your dataframes and time windows ready
    jank_data = [
        # (dataframe, start_time, end_time)
        # (df1, 178033926826254, 178033991530896),
        # (df2, 178029650111946, 178029667489339),
        # ... hundreds more
    ]
    
    # Create analyzer with dataframes
    # analyzer = MultiWindowSchedSliceAnalyzer.from_dataframes(jank_data)
    # analyzer.run_analysis()
    # analyzer.print_global_summary()
    
    # Method 2: Build analyzer step by step
    analyzer = MultiWindowSchedSliceAnalyzer()
    
    # Add individual dataframes
    # analyzer.add_dataframe_window(df1, start1, end1, "Jank_Frame_1")
    # analyzer.add_dataframe_window(df2, start2, end2, "Jank_Frame_2")
    
    # Or add multiple at once
    # analyzer.add_jank_dataframes(jank_data, ["Frame1", "Frame2", ...])
    
    # Method 3: Legacy file-based input (still supported)
    # analyzer.add_time_window('data1.tsv', start1, end1, 'Jank_1')
    # analyzer.add_jank_frames([('data1.tsv', start1, end1), ('data2.tsv', start2, end2)])
    
    print("✅ Improved MultiWindowSchedSliceAnalyzer ready!")
    print("📊 Now supports direct DataFrame input for better performance")
    print("🚀 No more file path dependencies!")

if __name__ == "__main__":
    example_usage()