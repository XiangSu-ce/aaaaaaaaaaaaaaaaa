import json
import os
import time
from typing import List


def _atomic_write(filename: str, text: str) -> None:
    """原子写文件（先写临时文件再 os.replace），保证热更新时读取端不会读到半截内容"""
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filename)


class ProxyStorage:
    @staticmethod
    def save_proxies_with_type(filename: str, normal_proxies: List[str], anonymous_proxies: List[str]) -> None:
        """
        保存代理IP到JSON文件（区分普通/高匿类型，兼容旧版格式）
        :param filename: 保存文件名
        :param normal_proxies: 有效普通代理列表
        :param anonymous_proxies: 有效高匿代理列表
        """
        total_count = len(normal_proxies) + len(anonymous_proxies)
        save_data = {
            "summary": {
                "normal_count": len(normal_proxies),
                "anonymous_count": len(anonymous_proxies),
                "total_count": total_count,
                "update_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            },
            "proxy_list": {
                "normal": normal_proxies,  # 普通代理IP列表（透明/普通匿名）
                "anonymous": anonymous_proxies  # 高匿代理IP列表（高匿名）
            }
        }

        _atomic_write(filename, json.dumps(save_data, ensure_ascii=False, indent=2))

        # 保存提示
        print(f"\n{'=' * 60}")
        print(f"💾 代理IP保存完成！")
        print(f"📂 保存路径：{filename}")
        print(f"📊 保存统计：")
        print(f"   ├─ 有效普通代理：{len(normal_proxies):5d} 个（透明/普通匿名）")
        print(f"   ├─ 有效高匿代理：{len(anonymous_proxies):5d} 个（高匿名）")
        print(f"   └─ 总计有效代理：{total_count:5d} 个")
        print(f"⏰ 最后更新时间：{save_data['summary']['update_time']}")
        print(f"{'=' * 60}")

    @staticmethod
    def save_to_json(filename: str, proxies: List[str]) -> None:
        """兼容方法：保存单一类型代理IP到JSON"""
        save_data = {
            "total": len(proxies),
            "update_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "proxies": proxies
        }

        _atomic_write(filename, json.dumps(save_data, ensure_ascii=False, indent=2))

        print(f"\n{'=' * 50}")
        print(f"💾 代理IP保存完成！")
        print(f"📂 保存路径：{filename}")
        print(f"📊 保存统计：共 {len(proxies):5d} 个代理IP")
        print(f"⏰ 最后更新时间：{save_data['update_time']}")
        print(f"{'=' * 50}")

    @staticmethod
    def load_state(filename: str) -> dict:
        """
        读取长期池状态：
        {
          "last_pool_check": "YYYY-MM-DD",       # 最近一次池内每日复检的日期
          "valid": {代理: 结果字典},               # 当前所有有效代理（长期保留，输出时按出口唯一）
          "seen":  {代理: 最后验证日(epoch天数)}    # 已测过的候选记录，避免重复扫描同一个IP
        }
        """
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("valid"), dict) and isinstance(data.get("seen"), dict):
                data.setdefault("last_pool_check", "")
                return data
        except (OSError, ValueError):
            pass
        return {"last_pool_check": "", "valid": {}, "seen": {}}

    @staticmethod
    def save_state(filename: str, state: dict) -> None:
        """原子写长期池状态"""
        _atomic_write(filename, json.dumps(state, ensure_ascii=False))

    @staticmethod
    def save_pool(json_filename: str, txt_filename: str, pool: List[dict], summary: dict) -> None:
        """
        保存最终IP池（原子写，支持热更新）
        :param json_filename: pool.json 路径（含延迟/出口IP/能力等详情，按延迟升序）
        :param txt_filename: pool.txt 路径（纯代理列表，一行一个，按延迟升序）
        :param pool: 最终代理详情列表
        :param summary: 本轮汇总信息
        """
        save_data = {"summary": summary, "proxies": pool}
        _atomic_write(json_filename, json.dumps(save_data, ensure_ascii=False, indent=2))

        lines = [item["proxy"] for item in pool]
        _atomic_write(txt_filename, ("\n".join(lines) + "\n") if lines else "")

        print(f"\n{'=' * 60}")
        print(f"💾 IP池热更新完成！（原子写，读取端可随时安全读取）")
        print(f"📂 JSON 详情：{json_filename}")
        print(f"📂 TXT 列表：{txt_filename}")
        print(f"📊 最终IP池统计：")
        print(f"   ├─ 唯一出口（对外输出）：{summary.get('total', len(pool)):5d} 个")
        if "valid_total" in summary:
            print(f"   ├─ 池内备用（同出口落选）：{summary['valid_total'] - summary.get('total', 0):5d} 个")
        if "rechecked" in summary:
            print(f"   ├─ 每日复检：通过 {summary.get('recheck_passed', 0):5d} 个 | 剔除失效 {summary.get('removed_dead', 0):5d} 个")
        if "new_passed" in summary:
            print(f"   ├─ 本轮新增入池：{summary.get('new_passed', 0):5d} 个")
        if "skipped_seen" in summary:
            print(f"   ├─ 跳过历史已测：{summary.get('skipped_seen', 0):5d} 个")
        print(f"   ├─ 同出口合并：{summary.get('merged_same_exit', 0):5d} 个")
        print(f"   ├─ 剔除透明代理：{summary.get('excluded_leaky', 0):5d} 个")
        print(f"   └─ 本轮耗时：{summary.get('cycle_seconds', 0):5d} 秒")
        print(f"⏰ 最后更新时间：{summary.get('updated_at', '')}")
        print(f"{'=' * 60}")
