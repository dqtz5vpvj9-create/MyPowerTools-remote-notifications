#!/bin/bash
# Wrapper to capture Codex hook stdin and forward it through the shared
# notification channel.
STDIN=$(cat)
echo "$STDIN" > /tmp/codex_hook_raw_stdin.json
echo "$STDIN" | /home/chris/miniconda3/envs/android_automatic_314/bin/python3 /android/androidtools/py_modules/send_notification.py --stdin --hook "$1" --client codex --icon codex > /tmp/codex_hook.log 2>&1 || true
exit 0
