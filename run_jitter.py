#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""随机化启动器：先随机等待一段时间，再以随机时长执行 main.py
用于规避"每天固定时刻、固定时长"这种一眼可辨的机器特征。

参数（秒/次数）：
  --delay-min / --delay-max   启动前随机等待的秒数，默认 0 ~ 2400（0~40 分钟）
  --num-min   / --num-max     阅读次数随机范围，默认 132 ~ 148（66~74 分钟）
"""
import argparse
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay-min", type=int, default=0)
    ap.add_argument("--delay-max", type=int, default=2400)
    ap.add_argument("--num-min", type=int, default=132)
    ap.add_argument("--num-max", type=int, default=148)
    a = ap.parse_args()

    delay = random.randint(a.delay_min, a.delay_max)
    num = random.randint(a.num_min, a.num_max)

    print(f"[jitter] 随机等待 {delay} 秒（{delay / 60:.1f} 分钟）后开始", flush=True)
    time.sleep(delay)

    env = dict(os.environ)
    env["READ_NUM"] = str(num)
    print(f"[jitter] 本次阅读 {num} 次，预计 {num * 0.5:.0f} 分钟", flush=True)

    sys.exit(subprocess.call([sys.executable, "main.py"], cwd=HERE, env=env))


if __name__ == "__main__":
    main()
