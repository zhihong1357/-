#!/usr/bin/env bash
# Mac / Linux：首次运行会自动安装依赖，之后一直循环扫描
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 scanner.py --loop
