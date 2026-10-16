import argparse
import os
import random
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

from config.proxy_sources import NORMAL_PROXIES, ANONYMOUS_PROXIES, EXTRA_SOURCES
from utils.crawler import ProxyCrawler
from utils.storage import ProxyStorage
from utils.validator import ProxyValidator

try:
    # Windows 控制台 / 重定向输出兼容（避免 emoji 编码崩溃）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

_STOP_FLAG = {"stop": False}


def _handle_signal(signum, frame):
    _STOP_FLAG["stop"] = True
    print(f"\n⚠️ 收到停止信号（{signum}），本轮结束后退出...")


def filter_sources(sources, only: str):
    """按关键字过滤源（逗号分隔，匹配源名称子串）"""
    if not only:
        return sources
    keywords = [k.strip().lower() for k in only.split(",") if k.strip()]
    return [s for s in sources if any(k in s["name"].lower() for k in keywords)]


def run_cycle(args, validator):
    """执行一轮：爬取 → 验证 → 剔透明 → 出口去重 → 按延迟排序 → 原子保存"""
    cycle_start = time.time()
    print(f"\n{'=' * 80}")
    print(f"🚀 新一轮任务开始 | {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'=' * 80}")

    proxy_with_type = []

    # 阶段1：并行爬取所有源（每个源一个独立的爬虫实例）
    crawl_tasks = []
    if args.type in ("all", "normal"):
        crawl_tasks += [(s, "normal") for s in filter_sources(NORMAL_PROXIES, args.only)]
    if args.type in ("all", "anonymous"):
        crawl_tasks += [(s, "anonymous") for s in filter_sources(ANONYMOUS_PROXIES, args.only)]
    if args.type == "all":
        crawl_tasks += [(s, "normal") for s in filter_sources(EXTRA_SOURCES, args.only)]

    if crawl_tasks:
        t_crawl = time.time()
        print(f"📥 [阶段1/3] 并行爬取 {len(crawl_tasks)} 个源（并发 {args.crawl_workers}）...")
        with ThreadPoolExecutor(max_workers=args.crawl_workers) as executor:
            futures = {}
            for source, cls_tag in crawl_tasks:
                crawler = ProxyCrawler()
                futures[executor.submit(crawler.crawl_source, source)] = (crawler, cls_tag, source)
            for future in as_completed(futures):
                crawler, cls_tag, source = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"❌ [{source['name']}] 抓取异常：{str(e)[:60]}")
                for ip in crawler.proxies:
                    proxy_with_type.append((ip, cls_tag))
        print(f"📥 全部源爬取完成 | 共 {len(proxy_with_type)} 条（未去重）| 耗时 {int(time.time() - t_crawl)}s")

    # 合并去重（同一代理 高匿 优先）
    type_by_proxy = {}
    for ip, type_tag in proxy_with_type:
        if ip in type_by_proxy and type_by_proxy[ip] == "anonymous":
            continue
        type_by_proxy[ip] = type_tag
    pending = list(type_by_proxy.keys())
    crawled_unique = len(pending)

    # 长期池状态：valid（池内有效代理，长期保留）+ seen（历史已测记录，防重复扫）
    state_file = os.path.join(args.output_dir, "pool_state.json")
    today = date.today().isoformat()
    today_day = int(time.time() // 86400)
    state = ProxyStorage.load_state(state_file)
    valid = state["valid"]   # 当前所有有效代理（输出时按出口唯一）
    seen = state["seen"]     # 已测过的候选记录 {代理: 最后验证日}
    last_pool_check = state["last_pool_check"]

    # 每日复检：当天首次运行（或 --full-scan）时，对池内所有代理逐一复扫
    daily_check = last_pool_check != today or args.full_scan
    recheck_list = list(valid.keys()) if daily_check else []

    # 新候选 = 本轮爬到的、从未测过且不在池内的IP；其余全部跳过，绝不重复扫
    in_pool = [p for p in pending if p in valid]
    if args.full_scan:
        new_candidates = [p for p in pending if p not in valid]
    else:
        new_candidates = [p for p in pending if p not in valid and p not in seen]
    skipped_seen = len(pending) - len(in_pool) - len(new_candidates)

    # 本轮汇总
    print("\n" + "=" * 60)
    print("📊 本轮汇总")
    print("=" * 60)
    print(f"   ├─ 本轮爬取（去重后）：{crawled_unique:5d} 个")
    print(f"   ├─ 池内已有、跳过：{len(in_pool):5d} 个")
    print(f"   ├─ 历史测过、跳过：{skipped_seen:5d} 个（同一IP不重复扫）")
    if daily_check:
        print(f"   ├─ 每日复检池内代理：{len(recheck_list):5d} 个（失效剔除/成功保留）")
    print(f"   └─ 新增待验证：{len(new_candidates):5d} 个")
    print("=" * 60 + "\n")

    if args.full_scan:
        print("🔄 --full-scan：忽略历史记录，强制全量重扫（含池内复检）")
    if args.max_validate and len(new_candidates) > args.max_validate:
        random.shuffle(new_candidates)
        new_candidates = new_candidates[: args.max_validate]
        print(f"⚠️ 新候选已随机抽样 {args.max_validate} 个进行验证（--max-validate 限制）\n")

    # 阶段2：批量验证（延迟 + 出口IP）：新候选 + 每日复检的池内代理
    to_validate = new_candidates + recheck_list
    if to_validate:
        results = validator.validate(to_validate, thread_count=args.threads,
                                     fast_timeout=args.fast_timeout)
    else:
        results = []
        print("ℹ️ 本轮没有需要验证的IP（无新候选，且今日已复检过池内代理）")

    # 剔除透明代理（出口IP == 本机真实出口IP）
    own_ip = validator.get_own_ip()
    leaky_count = 0
    if own_ip:
        before_filter = len(results)
        results = [r for r in results if r["exit_ip"] != own_ip]
        leaky_count = before_filter - len(results)
        print(f"🛡️ 本机出口IP：{own_ip} | 已剔除透传自身出口的代理：{leaky_count} 个")
    else:
        print("⚠️ 无法获取本机出口IP，跳过透明代理剔除")

    # 更新长期池：复检通过的刷新数据（保留原cls）；新通过的直接入池
    recheck_set = set(recheck_list)
    passed_new, recheck_ok = 0, 0
    for r in results:
        r["checked_at"] = today
        if r["proxy"] in recheck_set:
            valid[r["proxy"]].update(r)  # cls/added_at 保留
            recheck_ok += 1
        else:
            r["cls"] = type_by_proxy.get(r["proxy"], "normal")
            r["added_at"] = today
            valid[r["proxy"]] = r
            passed_new += 1

    # 未通过的：复检失效 → 从池中剔除；全部记入 seen，不再重复扫
    passed_proxies = {r["proxy"] for r in results}
    failed = [p for p in to_validate if p not in passed_proxies]
    removed_dead = 0
    if own_ip is None and recheck_list:
        kept = [p for p in failed if p in recheck_set]
        failed = [p for p in failed if p not in recheck_set]
        if kept:
            print(f"⚠️ 本机出口IP获取失败（疑似网络异常），保守起见本轮不移除复检失败的 {len(kept)} 个池内代理")
    for p in failed:
        if p in recheck_set and p in valid:
            del valid[p]
            removed_dead += 1
        seen[p] = today_day

    # 输出池 = 当前所有有效代理按出口唯一（同出口保留延迟最低），按延迟升序
    entries = list(valid.values())
    by_exit = {}
    for r in entries:
        key = r["exit_ip"] or r["proxy"]
        current = by_exit.get(key)
        if current is None or r["latency_ms"] < current["latency_ms"]:
            by_exit[key] = r
    merged_same_exit = len(entries) - len(by_exit)
    final = sorted(by_exit.values(), key=lambda r: r["latency_ms"])

    # 阶段3：原子保存（热更新）
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 清理过期的历史已测记录（保留 seen-retention-days 天，过期后若上游再现会作为新候选）
    retention = max(1, args.seen_retention_days)
    before_prune = len(seen)
    seen = {k: v for k, v in seen.items() if today_day - v <= retention}
    seen_pruned = before_prune - len(seen)

    summary = {
        "total": len(final),
        "unique_exits": len(final),
        "valid_total": len(valid),
        "seen_total": len(seen),
        "crawled": len(proxy_with_type),
        "crawled_unique": crawled_unique,
        "skipped_in_pool": len(in_pool),
        "skipped_seen": skipped_seen,
        "rechecked": len(recheck_list),
        "recheck_passed": recheck_ok,
        "removed_dead": removed_dead,
        "new_tested": len(new_candidates),
        "new_passed": passed_new,
        "excluded_leaky": leaky_count,
        "merged_same_exit": merged_same_exit,
        "seen_pruned": seen_pruned,
        "updated_at": updated_at,
        "cycle_seconds": int(time.time() - cycle_start),
    }

    pool_json = os.path.join(args.output_dir, "pool.json")
    pool_txt = os.path.join(args.output_dir, "pool.txt")
    legacy_json = os.path.join(args.output_dir, "proxy_ip.json")

    ProxyStorage.save_pool(pool_json, pool_txt, final, summary)

    normal_list = [r["proxy"] for r in final if r["cls"] != "anonymous"]
    anonymous_list = [r["proxy"] for r in final if r["cls"] == "anonymous"]
    ProxyStorage.save_proxies_with_type(legacy_json, normal_list, anonymous_list)

    # 保存长期池状态（valid + seen + 最近复检日期）
    # 网络异常导致复检未生效时，不更新复检日期，下一轮会重试每日复检
    state = {
        "updated_at": updated_at,
        "last_pool_check": today if (own_ip or not recheck_list) else last_pool_check,
        "valid": valid,
        "seen": seen,
    }
    ProxyStorage.save_state(state_file, state)
    print(f"💡 长期池状态已保存：{state_file}（池内代理 {len(valid)} 个 | 历史记录 {len(seen)} 个）")

    if final:
        print(f"\n⚡ 延迟最低 Top5：")
        for r in final[:5]:
            caps = ",".join(r["caps"])
            print(f"   {r['proxy']:<24} {r['latency_ms']:>6d} ms  出口 {r['exit_ip']}  能力 {caps}")


def main():
    parser = argparse.ArgumentParser(
        description="免费代理IP池：抓取 → 验证 → 出口去重 → 按延迟排序 → 热更新（默认每 30 分钟一轮）"
    )
    parser.add_argument("--interval", type=int, default=1800, help="轮询间隔（秒），默认 1800（30分钟）")
    parser.add_argument("--threads", type=int, default=1000, help="并发验证线程数，默认 1000（大池首轮建议 2000-3000）")
    parser.add_argument("--timeout", type=float, default=8, help="HTTPS 复核/完整验证的单代理超时（秒），默认 8")
    parser.add_argument("--fast-timeout", type=float, default=4,
                        help="两段式快筛(HTTP)的单代理超时（秒），默认 4；设 0 关闭快筛（慢但更全）")
    parser.add_argument("--crawl-workers", type=int, default=8, help="源爬取并发数，默认 8")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出（适合 cron）")
    parser.add_argument("--max-validate", type=int, default=0, help="每轮最多验证的数量（随机抽样），0=不限制")
    parser.add_argument("--type", choices=["all", "normal", "anonymous"], default="all", help="抓取类型，默认 all")
    parser.add_argument("--only", default="", help="只抓名称包含关键字的源，逗号分隔，如：Proxora,GeoNode,SCDN")
    parser.add_argument("--output-dir", default=".", help="输出目录，默认当前目录")
    parser.add_argument("--seen-retention-days", type=int, default=7,
                        help="历史已测记录保留天数（过期后若上游再现该IP会重新作为新候选验证），默认7")
    parser.add_argument("--full-scan", action="store_true",
                        help="忽略历史记录强制全量重扫（含池内复检），一般不需要")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    validator = ProxyValidator(timeout=args.timeout)

    print("=" * 80)
    print("🚀 免费代理IP池 · 守护进程")
    print("=" * 80)
    print(f"   ├─ 轮询间隔：{args.interval} 秒")
    print(f"   ├─ 验证线程：{args.threads} | 单代理超时：{args.timeout}s")
    print(f"   ├─ 抓取类型：{args.type}" + (f" | 仅限源：{args.only}" if args.only else ""))
    print(f"   ├─ 抽样验证：{args.max_validate if args.max_validate else '不限制'}")
    print(f"   └─ 输出目录：{os.path.abspath(args.output_dir)}")
    print("=" * 80)

    while not _STOP_FLAG["stop"]:
        cycle_start = time.time()
        try:
            run_cycle(args, validator)
        except Exception:
            print("❌ 本轮任务异常：")
            traceback.print_exc()

        if args.once or _STOP_FLAG["stop"]:
            break

        next_start = cycle_start + args.interval
        wait_seconds = next_start - time.time()
        if wait_seconds > 0:
            nxt = datetime.fromtimestamp(next_start).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n⏰ 本轮完成，{int(wait_seconds // 60)} 分钟后（预计 {nxt}）开始下一轮...\n")
            while wait_seconds > 0 and not _STOP_FLAG["stop"]:
                time.sleep(min(5, wait_seconds))
                wait_seconds = next_start - time.time()
        else:
            print(f"\n⚠️ 本轮耗时 {-wait_seconds:.0f}s 超出轮询间隔，立即开始下一轮\n")

    print("👋 程序已退出")


if __name__ == "__main__":
    main()
