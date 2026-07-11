\subsection{End-to-End Performance}
\label{subsec:eval-e2e}

\textbf{Task Completion Time.} 
Out of 116 AndroidWorld tasks, both Vanilla and \ACIntAbbr achieve an identical success rate (SR@3) of 76/116 (65.5\%), with 73 common successes. 
Across these 73 tasks, the mean step count difference is -0.51 (\ACIntAbbr $-$ Vanilla). 
\ACIntAbbr reduces the mean per-task time from 30.88\,s to 21.08\,s, a 1.47$\times$ speedup. 
This improvement comes predominantly from a 1.78$\times$ reduction in non-LLM time (21.54\,s to 12.07\,s), while LLM inference time remains effectively unchanged (9.35\,s to 9.01\,s). 
The latency reduction is more pronounced for longer tasks: the 90th percentile latency drops from 58.55\,s to 37.67\,s, indicating substantial savings in long-tail tasks.

\textbf{Correctness.}
A natural concern is that early completion could cause the VLM to observe stale screens, leading the agent down an incorrect path.
We inspected the traces where Vanilla and \ACIntAbbr diverged in success (6 tasks) or step count (23 out of the 73 common tasks) and found:
The small variations in task completion and step counts stem primarily from the inherent non-determinism of the VLM output, including the progress summary and navigation path choices.
Screenshots captured by \ACInt consistently show a fully rendered UI, with no evidence of incomplete or stale screens (e.g., loading indicators, partially rendered views).
These findings confirm that action completion correctness is not degraded under \ACInt.

\textbf{Matching Effectiveness.}
To understand where the speedup originates, we analyze per-action wait outcomes across the 574 actions from the 73 commonly-successful tasks in the \ACIntAbbr run.
Among them, 386 (67.2\%) exit early, 146 (25.4\%) encounter database hits without timely completion, and 42 (7.3\%) miss database coverage and fall back to fixed waits.

% ---------------------------------------------------------------------------
\textbf{CPU and Power Efficiency}
\label{subsec:eval-power}

Total CPU usage across the 73 commonly-successful tasks decreases from 1,673 core-seconds (Vanilla) to 1,448 core-seconds (\ACIntAbbr), a 13.5\% reduction. 
This decrease stems primarily from animation suppression: 
\texttt{SurfaceFlinger} CPU time drops by 51.0\% (158\,s\,$\to$\,77\,s).
The target application process itself drops by 4.8\% (371\,s\,$\to$\,354\,s), reflecting slightly less work due to fewer rendered transition frames. 
We also measured energy consumption.
Across the 73 tasks, total energy drops from 538\,Wh under Vanilla to 435\,Wh under \ACIntAbbr, a 19.1\% reduction. 
This reduction exceeds the CPU improvement because the shorter active time per task also reduces non-computation energy overheads (e.g., storage, radios).

% ---------------------------------------------------------------------------
\subsection{Effectiveness of OS Supports}
\label{subsec:eval-ablation}

\textbf{Effect of \SCF.}
We isolate the impact of \SCF pruning by evaluating a variant that lacks synchronous-completion fence pruning.
Without \SCF, the early exit rate drops to 43.0\% and mean latency increases to 27.00\,s.

\textbf{Effect of \VPF.}
We measure the gap between logical completion and visual presentation across all early exits in \ACIntAbbr.
The gap distribution is P50\,=\,44.1\,ms, P90\,=\,68.9\,ms, P99\,=\,199.6\,ms, with a maximum of 435.0\,ms.
This highlights the necessity of \VPF: without it, a screenshot taken immediately after logical completion could precede the committed frame by up to hundreds of milliseconds, causing the VLM to read a transitional state on transition-heavy actions.