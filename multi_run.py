"""分时段随机阅读编排器。

把「一天 70~80 分钟」拆成 3~5 段，在指定的时间窗口（默认北京时间 06:00-10:00）
内随机挑选各段的开始时刻，段与段之间留随机间隔，段内请求数也随机。
相比「一次性连刷 70 分钟」，这种形态在服务端看来更接近真人零散翻书。

用法：
    python multi_run.py                      # 默认 06:00-10:00，总量 70~80 分钟
    python multi_run.py --total-min 75       # 指定总量
    python multi_run.py --start-hour 6 --end-hour 10 --segments 4
    python multi_run.py --dry-run            # 只打印排程不执行
"""
import argparse
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# 中国无夏令时，直接用固定 +8 偏移，避免 Windows 上缺 tzdata 时 zoneinfo 报错
TZ = timezone(timedelta(hours=8))
SECONDS_PER_REQUEST = 30.0          # 每个请求服务端记 30 秒
AVG_INTERVAL = 33.5                 # main.py 的随机间隔 26~41 秒，均值约 33.5
SEGMENT_OVERHEAD = 8.0              # 每段启动时刷新 cookie 等固定开销（秒）
SEGMENT_GAP_MIN = 90                # 段与段之间最小间隔（秒）
TAIL_MARGIN = 120                   # 最后一段结束后窗口内的余量（秒）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(BASE_DIR, "main.py")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("multi")


def split_requests(total, k, min_part=None):
    """把 total 次请求随机拆成 k 段，每段不少于 min_part 次。"""
    if min_part is None:
        # 总量很小时自动放宽下限，避免退化成单段
        min_part = max(2, min(20, total // 4))
    k = max(1, min(k, total // min_part)) if total >= min_part else 1
    for _ in range(200):
        weights = [random.random() + 0.4 for _ in range(k)]
        s = sum(weights)
        parts = [max(min_part, int(round(total * w / s))) for w in weights]
        diff = total - sum(parts)
        guard = 0
        while diff != 0 and guard < 500:
            guard += 1
            j = random.randrange(k)
            if diff > 0:
                parts[j] += 1
                diff -= 1
            elif parts[j] > min_part:
                parts[j] -= 1
                diff += 1
        if sum(parts) == total:
            return parts
    parts = [total // k] * k
    for i in range(total - sum(parts)):
        parts[i % k] += 1
    return parts


def build_schedule(args):
    """生成 [(开始时间戳, 请求次数), ...] 排程。"""
    total_min = args.total_min or random.randint(70, 80)
    total_req = int(round(total_min * 60 / SECONDS_PER_REQUEST))
    k = args.segments or random.randint(3, 5)

    parts = split_requests(total_req, k)
    k = len(parts)
    durations = [p * AVG_INTERVAL + SEGMENT_OVERHEAD for p in parts]
    need = sum(durations)

    now = datetime.now(TZ)
    if args.now:
        # 立即开跑（补刷/测试用）：窗口压缩成「刚好容下所需时长」，段间只留最小间隔
        window_start = now
        window_end = now + timedelta(seconds=need + SEGMENT_GAP_MIN * k + 60)
    else:
        window_start = now.replace(hour=args.start_hour, minute=0, second=0, microsecond=0)
        window_end = now.replace(hour=args.end_hour, minute=0, second=0, microsecond=0)
        if window_end <= window_start:
            window_end = window_start + timedelta(hours=max(1, args.end_hour - args.start_hour))

        # 任务启动得比窗口开始晚（例如 GitHub 排队延迟），就从当前时刻开始
        if now > window_start:
            window_start = now
        if window_start >= window_end:
            # 已经超出窗口，退化为连续执行
            window_end = window_start + timedelta(seconds=need + 300)

    window_sec = (window_end - window_start).total_seconds()
    slack = window_sec - need - TAIL_MARGIN
    if slack < SEGMENT_GAP_MIN * (k + 1):
        slack = max(0.0, window_sec - need - 30)

    # 把空闲时间随机分配到「首段前、段间、末段后」共 k+1 个间隙
    if slack > 0:
        weights = [random.random() + 0.25 for _ in range(k + 1)]
        wsum = sum(weights)
        gaps = [slack * w / wsum for w in weights]
        # 保证段间有最小间隔
        for i in range(1, k):
            if gaps[i] < SEGMENT_GAP_MIN and gaps[i] > 0:
                short = SEGMENT_GAP_MIN - gaps[i]
                gaps[i] += short
                gaps[0] = max(0.0, gaps[0] - short)
    else:
        gaps = [0.0] + [SEGMENT_GAP_MIN] * (k - 1) + [0.0]

    schedule = []
    cursor = window_start.timestamp() + gaps[0]
    for i, p in enumerate(parts):
        schedule.append((cursor, p))
        cursor += durations[i] + (gaps[i + 1] if i + 1 < len(gaps) else 0)

    return total_min, schedule


def run_segment(idx, total_seg, ts, requests):
    start = datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
    log.info("=== 第 %d/%d 段：%s 开跑，%d 次 ≈ %.1f 分钟 ===",
             idx, total_seg, start, requests, requests * SECONDS_PER_REQUEST / 60)
    wait = ts - time.time()
    if wait > 0:
        log.info("等待 %d 分 %02d 秒...", int(wait // 60), int(wait % 60))
        time.sleep(wait)

    env = os.environ.copy()
    env["READ_NUM"] = str(requests)
    # 分段执行时不单独推送，由编排器最后汇总推一条
    env["PUSH_METHOD"] = ""

    proc = subprocess.run([sys.executable, MAIN_PY], cwd=BASE_DIR, env=env)
    end = datetime.now(TZ).strftime("%H:%M")
    if proc.returncode == 0:
        log.info("第 %d 段完成（%s 结束），记入 %.1f 分钟",
                 idx, end, requests * SECONDS_PER_REQUEST / 60)
        return 0.0
    log.error("第 %d 段异常退出，返回码 %s", idx, proc.returncode)
    return -1.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-hour", type=int, default=6)
    parser.add_argument("--end-hour", type=int, default=10)
    parser.add_argument("--total-min", type=int, default=0, help="0 表示 70~80 随机")
    parser.add_argument("--segments", type=int, default=0, help="0 表示 3~5 段随机")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--now", action="store_true",
                        help="立即开跑，段间只留最小间隔（补刷或测试用）")
    args = parser.parse_args()

    total_min, schedule = build_schedule(args)
    plan_total = sum(p for _, p in schedule) * SECONDS_PER_REQUEST / 60

    log.info("排程：%d 段，共 %d 次，计划累计 %.1f 分钟（目标 %d 分钟）",
             len(schedule), sum(p for _, p in schedule), plan_total, total_min)
    for i, (ts, p) in enumerate(schedule, 1):
        st = datetime.fromtimestamp(ts, TZ)
        en = st + timedelta(seconds=p * AVG_INTERVAL + SEGMENT_OVERHEAD)
        log.info("  第 %d 段 %s - %s，%d 次（%.1f 分钟）",
                 i, st.strftime("%H:%M"), en.strftime("%H:%M"), p,
                 p * SECONDS_PER_REQUEST / 60)

    if args.dry_run:
        return 0

    done_min = 0.0
    failed = 0
    for i, (ts, p) in enumerate(schedule, 1):
        got = run_segment(i, len(schedule), ts, p)
        if got < 0:
            failed += 1
            if failed >= 2:
                log.error("连续分段失败，终止本次任务")
                break
        else:
            done_min += p * SECONDS_PER_REQUEST / 60

    from push import push
    from config import PUSH_METHOD

    if PUSH_METHOD:
        if failed:
            msg = (f"微信读书自动阅读部分失败。\n成功 {done_min:.1f} 分钟，"
                   f"{failed} 段异常，请查看 Actions 日志。")
            push(msg, PUSH_METHOD, is_success=False)
        else:
            times = " / ".join(datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
                               for ts, _ in schedule)
            msg = (f"微信读书自动阅读完成。\n分 {len(schedule)} 段执行：{times}\n"
                   f"累计阅读时长：{done_min:.1f} 分钟。")
            push(msg, PUSH_METHOD, is_success=True)
    else:
        log.info("未配置推送渠道，跳过推送。")

    log.info("全部结束，累计 %.1f 分钟，失败 %d 段", done_min, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
