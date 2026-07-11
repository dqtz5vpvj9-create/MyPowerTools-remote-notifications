#!/usr/bin/env python3
"""Extract android_logs from a Perfetto trace and serialize them like logcat."""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


PRIORITY_TO_CHAR: Dict[int, str] = {
    1: "S",  # silent
    2: "V",
    3: "D",
    4: "I",
    5: "W",
    6: "E",
    7: "F",
    8: "F",  # guard for perfetto variants
}

CHAR_TO_PRIORITY: Dict[str, int] = {v: k for k, v in PRIORITY_TO_CHAR.items()}
CHAR_TO_PRIORITY.update({"A": 7})  # treat "Assert" as fatal


def _priority_letter(value: Optional[int]) -> str:
    if value is None:
        return "?"
    return PRIORITY_TO_CHAR.get(int(value), "?")


def _priority_floor(letter: Optional[str]) -> Optional[int]:
    if not letter:
        return None
    letter = letter.strip().upper()
    return CHAR_TO_PRIORITY.get(letter)


def _format_log_line(
    raw_ts: int,
    prio: Optional[int],
    utid: Optional[int],
    tag: Optional[str],
    message: str,
    use_utc: bool,
) -> List[str]:
    # Perfetto timestamps are in nanoseconds; convert to datetime.
    ts_seconds = raw_ts / 1_000_000_000
    tzinfo = timezone.utc if use_utc else None
    dt = datetime.fromtimestamp(ts_seconds)
    date_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    utid_str = f"{utid:5d}" if utid is not None else "    -"
    prio_char = _priority_letter(prio)
    safe_tag = (tag or "-").strip()
    # Preserve newlines by indenting continued lines similar to logcat dumps.
    lines = message.rstrip("\n").splitlines() or [""]
    formatted: List[str] = []
    for idx, line in enumerate(lines):
        prefix = f"{date_str} {utid_str} {prio_char} {safe_tag}: "
        if idx == 0:
            formatted.append(prefix + line)
        else:
            formatted.append(" " * len(prefix) + line)
    return formatted


class PerfettoLogAnalyzer:
    """Fetch and format android_logs table data from Perfetto traces."""

    def __init__(self, trace_processor_path: Optional[str] = None, use_utc: bool = True):
        self.trace_processor_path = (
            trace_processor_path or os.path.expanduser("~/.local/bin/trace_processor")
        )
        self.use_utc = use_utc
        self._validate_trace_processor()

    def _validate_trace_processor(self) -> None:
        if not os.path.exists(self.trace_processor_path):
            raise FileNotFoundError(
                f"trace_processor binary not found at {self.trace_processor_path}. "
                "Install Perfetto tools or provide the path with --trace-processor."
            )

    @staticmethod
    def _get_trace_bounds(tp: TraceProcessor) -> Tuple[int, int]:
        bounds = tp.query("SELECT start_ts, end_ts FROM trace_bounds").as_pandas_dataframe()
        if bounds.empty:
            raise ValueError("Trace bounds are missing")
        start_ts = int(bounds["start_ts"].iloc[0])
        end_ts = int(bounds["end_ts"].iloc[0])
        if end_ts <= start_ts:
            raise ValueError("Trace duration must be positive")
        return start_ts, end_ts

    @staticmethod
    def _build_tag_filter(tags: Optional[Sequence[str]]) -> str:
        if not tags:
            return ""
        unique = []
        for tag in tags:
            value = tag.strip()
            if value and value not in unique:
                unique.append(value)
        if not unique:
            return ""
        quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in unique)
        return f"AND tag IN ({quoted})"

    def _query_android_logs(
        self,
        tp: TraceProcessor,
        start_ts: int,
        end_ts: int,
        tags: Optional[Sequence[str]],
        min_priority: Optional[int],
        message_filter: Optional[str],
        limit: Optional[int],
    ) -> pd.DataFrame:
        tag_clause = self._build_tag_filter(tags)
        msg_clause = ""
        if message_filter:
            msg_pattern = message_filter.replace("'", "''")
            msg_clause = f"AND msg LIKE '%{msg_pattern}%'"

        prio_clause = ""
        if min_priority is not None:
            prio_clause = f"AND prio >= {int(min_priority)}"

        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""


        tmp = tp.query('SELECT * FROM clock_snapshot;').as_pandas_dataframe()
        print("Clock snapshots:\n%s", tmp.head())
        monotic_ts = tmp[tmp['clock_name'] == 'MONOTONIC']['clock_value'].values[0]
        realtime_ts = tmp[tmp['clock_name'] == 'REALTIME']['clock_value'].values[0]
        boottime_ts = tmp[tmp['clock_name'] == 'BOOTTIME']['clock_value'].values[0]

        query = f"""
        SELECT
            *
        FROM android_logs
        WHERE ts >= {start_ts}
          AND ts <= {end_ts}
          {tag_clause}
          {msg_clause}
          {prio_clause}
        ORDER BY ts
        {limit_clause}
        """

        ret = tp.query(query).as_pandas_dataframe()

        ret['ts'] = ret['ts'] - boottime_ts + realtime_ts

        # ['id', 'type', 'ts', 'utid', 'prio', 'tag', 'msg']

        return ret

    def export_logs(
        self,
        trace_path: str,
        output_path: Path,
        start_s: Optional[float] = None,
        end_s: Optional[float] = None,
        tags: Optional[Sequence[str]] = None,
        min_priority: Optional[str] = None,
        message_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Tuple[int, int, int, int]:
        trace_path = os.path.expanduser(trace_path)
        if not os.path.exists(trace_path):
            raise FileNotFoundError(f"Trace file not found: {trace_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        min_priority_value = _priority_floor(min_priority)

        tp = TraceProcessor(
            trace=trace_path,
            config=TraceProcessorConfig(bin_path=self.trace_processor_path),
        )
        try:
            trace_start_ts, trace_end_ts = self._get_trace_bounds(tp)
            start_ts = trace_start_ts if start_s is None else trace_start_ts + int(start_s * 1e9)
            end_ts = trace_end_ts if end_s is None else trace_start_ts + int(end_s * 1e9)
            if end_ts <= start_ts:
                raise ValueError("Requested time window is empty")

            df = self._query_android_logs(
                tp,
                start_ts=start_ts,
                end_ts=end_ts,
                tags=tags,
                min_priority=min_priority_value,
                message_filter=message_filter,
                limit=limit,
            )

            if df.empty:
                output_path.write_text("", encoding="utf-8")
                return 0, trace_start_ts, start_ts, end_ts

            lines: List[str] = []
            # ['id', 'type', 'ts', 'utid', 'prio', 'tag', 'msg']
            for _, row in df.iterrows():
                raw_ts = int(row.get("ts", 0))
                type = row.get("type", "")
                utid = row.get("utid", "")
                prio = int(row["prio"]) if pd.notnull(row.get("prio")) else None
                tag = str(row["tag"]) if pd.notnull(row.get("tag")) else None
                msg = str(row["msg"]) if pd.notnull(row.get("msg")) else ""
                formatted_lines = _format_log_line(
                    raw_ts=raw_ts,
                    prio=prio,
                    utid=utid,
                    tag=tag,
                    message=msg,
                    use_utc=self.use_utc,
                )
                lines.extend(formatted_lines)

            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return len(lines), trace_start_ts, start_ts, end_ts
        finally:
            tp.close()


def parse_tag_list(tag_string: Optional[str]) -> List[str]:
    tags: List[str] = []
    if not tag_string:
        return tags
    for part in tag_string.split(","):
        value = part.strip()
        if value and value not in tags:
            tags.append(value)
    return tags


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump android_logs from a Perfetto trace into a logcat-style file",
    )

    parser.add_argument("-t", "--trace", required=True, help="Path to the Perfetto trace file")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="",
        help="Output file path (default: perfetto_log_dump.txt)",
    )
    parser.add_argument(
        "--start",
        type=float,
        help="Start time in seconds from the beginning of the trace",
    )
    parser.add_argument(
        "--end",
        type=float,
        help="End time in seconds from the beginning of the trace",
    )
    parser.add_argument(
        "--trace-processor",
        type=str,
        help="Path to trace_processor binary (default: ~/.local/bin/trace_processor)",
    )
    parser.add_argument(
        "--tags",
        type=str,
        help="Comma-separated list of log tags to include",
    )
    parser.add_argument(
        "--min-priority",
        type=str,
        help="Minimum priority letter to include (e.g., D, I, W, E)",
    )
    parser.add_argument(
        "--contains",
        type=str,
        help="Filter logs whose message contains the given substring",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of log rows to fetch (for quick sampling)",
    )
    parser.add_argument(
        "--local-time",
        action="store_true",
        help="Format timestamps using local timezone instead of UTC",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    else:
        logging.getLogger().setLevel(logging.WARNING)

    if args.start is not None and args.start < 0:
        logger.error("Start time must be non-negative")
        sys.exit(1)
    if args.end is not None and args.end < 0:
        logger.error("End time must be non-negative")
        sys.exit(1)
    if args.start is not None and args.end is not None and args.end <= args.start:
        logger.error("End time must be greater than start time")
        sys.exit(1)
    if args.limit is not None and args.limit <= 0:
        logger.error("Limit must be a positive integer")
        sys.exit(1)

    if args.min_priority and _priority_floor(args.min_priority) is None:
        logger.error("Invalid minimum priority %s. Use one of V/D/I/W/E/F.", args.min_priority)
        sys.exit(1)

    tags = parse_tag_list(args.tags)
    analyzer = PerfettoLogAnalyzer(args.trace_processor, use_utc=not args.local_time)

    if args.output != '':
      output_path = Path(args.output)
    else:
      # output_path 设为tracepath父目录下的log_dump.txt
      output_path = Path(args.trace).parent / "log_dump.txt"

    try:
        line_count, trace_start_ts, start_ts, end_ts = analyzer.export_logs(
            trace_path=args.trace,
            output_path=output_path,
            start_s=args.start,
            end_s=args.end,
            tags=tags,
            min_priority=args.min_priority,
            message_filter=args.contains,
            limit=args.limit,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Log extraction failed: %s", exc)
        if args.debug:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    print("\n=== Perfetto Log Extraction Summary ===")
    print(f"Trace: {args.trace}")
    window_start_offset = (start_ts - trace_start_ts) / 1e9
    window_end_offset = (end_ts - trace_start_ts) / 1e9
    print(
        f"Window: {window_start_offset:.3f}s -> {window_end_offset:.3f}s "
        f"(duration {(end_ts - start_ts) / 1e9:.3f}s)"
    )
    print(f"Lines written: {line_count}")
    print(f"Output: {output_path}")

    if line_count == 0:
        print("No android_logs rows matched the provided filters.")
    else:
        try:
            preview_lines = output_path.read_text(encoding="utf-8").splitlines()[:10]
            if preview_lines:
                print("\nPreview:")
                for line in preview_lines:
                    print(line)
        except OSError:
            pass


if __name__ == "__main__":
    main()