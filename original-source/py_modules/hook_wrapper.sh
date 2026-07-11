#!/bin/bash
# Wrapper to capture stdin and forward to Python script
STDIN=$(cat)
echo "$STDIN" > /tmp/claude_hook_raw_stdin.json
echo "$STDIN" | /home/chris/miniconda3/envs/android_automatic_314/bin/python3 /android/androidtools/py_modules/send_notification.py --stdin --hook "$1" --icon claude > /tmp/claude_hook.log 2>&1
