import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    import socks  # noqa: F401  PySocks：socks4/socks5 代理支持
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False


class ProxyValidator:
    """代理IP验证类：多线程验证有效性，同时测量延迟与出口IP"""

    def __init__(self, timeout: float = 8):
        self.timeout = timeout
        # 验证请求头
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
        }
        self.https_url = "https://api.ipify.org?format=json"
        self.http_url = "http://api.ipify.org?format=json"

    @staticmethod
    def parse_proxy(proxy: str):
        """解析代理字符串 → (scheme, host, port)；非法返回 None（支持 socks4:// socks5:// 前缀）"""
        scheme, rest = "http", proxy or ""
        if "://" in proxy:
            scheme, rest = proxy.split("://", 1)
            scheme = scheme.lower()
        if ":" not in rest:
            return None
        host, port_str = rest.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return None
        if not (1 <= port <= 65535):
            return None
        return scheme, host, port

    def get_own_ip(self) -> str:
        """获取本机真实出口IP（直连），用于剔除透传自身出口的无效代理。
        多服务兜底 + 重试，避免单点瞬时失败导致跳过透明代理剔除。"""
        targets = (
            (self.https_url, "json"),
            (self.http_url, "json"),
            ("https://api.myip.com", "json"),
            ("http://ifconfig.me/ip", "text"),
        )
        for url, kind in targets:
            for _ in range(2):
                try:
                    resp = requests.get(url, headers=self.headers, timeout=self.timeout)
                    if kind == "json":
                        ip = resp.json().get("ip")
                    else:
                        ip = resp.text.strip()
                    if ip:
                        return ip
                except Exception:
                    continue
        return None

    def _probe(self, proxy_url: str, target: str, timeout: float = None):
        """通过代理探测目标URL，返回 (是否成功, 延迟ms, 出口IP)"""
        start = time.time()
        try:
            resp = requests.get(
                target,
                headers=self.headers,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout or self.timeout
            )
            latency = int((time.time() - start) * 1000)
            if resp.status_code == 200:
                try:
                    exit_ip = resp.json().get("ip")
                except ValueError:
                    exit_ip = None
                if exit_ip:
                    # 必须真实回显出口IP，才算验证通过（防止代理返回自称的200页面）
                    return True, latency, exit_ip
        except Exception:
            pass
        return False, int((time.time() - start) * 1000), None

    def check_proxy(self, proxy: str):
        """验证单个代理：HTTPS 优先、失败再试 HTTP；返回详情字典或 None"""
        parsed = self.parse_proxy(proxy)
        if not parsed:
            return None
        scheme, host, port = parsed
        if scheme.startswith("socks") and not HAS_SOCKS:
            return None
        proxy_url = f"{scheme}://{host}:{port}"

        caps, latencies, exit_ip = [], [], None
        ok, latency, ip = self._probe(proxy_url, self.https_url)
        if ok:
            caps.append("https")
            latencies.append(latency)
            exit_ip = ip
        ok, latency, ip = self._probe(proxy_url, self.http_url)
        if ok:
            caps.append("http")
            latencies.append(latency)
            exit_ip = exit_ip or ip

        if not caps:
            return None
        return {
            "proxy": proxy,
            "ip": host,
            "port": port,
            "protocol": scheme,
            "caps": caps,
            "latency_ms": min(latencies),
            "exit_ip": exit_ip,
        }

    def _http_probe_raw(self, proxy_ip: str, proxy_port: int, total_timeout: float):
        """
        原生 socket 的 HTTP 探测：硬性总时限（requests 的 read timeout 只是字节间隔
        超时，慢速滴流的蜜罐代理会吊着连接拖垮扫描速度）。
        返回 (是否成功, 延迟ms, 出口IP)
        """
        start = time.time()
        sock = None
        try:
            sock = socket.create_connection((proxy_ip, proxy_port), timeout=total_timeout)
            request = (
                f"GET {self.http_url} HTTP/1.1\r\n"
                f"Host: api.ipify.org\r\n"
                f"User-Agent: {self.headers['User-Agent']}\r\n"
                f"Accept: */*\r\n"
                f"Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode())
            deadline = start + total_timeout
            chunks = []
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    return False, 0, None
                sock.settimeout(remain)
                data = sock.recv(4096)
                if not data:
                    return False, 0, None
                chunks.append(data)
                text = b"".join(chunks).decode("utf-8", "replace")
                if " 200 " in text.split("\r\n", 1)[0]:
                    m = re.search(r'\{\s*"ip"\s*:\s*"(\d+\.\d+\.\d+\.\d+)"\s*\}', text)
                    if m:
                        return True, int((time.time() - start) * 1000), m.group(1)
        except Exception:
            return False, 0, None
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _http_connect_raw(self, proxy_ip: str, proxy_port: int, total_timeout: float):
        """
        原生 socket 的 HTTPS CONNECT 探测：向代理发起 CONNECT api.ipify.org:443，
        返回 200 即通过快筛（真实连通性由后续 HTTPS 复核验证，防止假 200 代理混入）。
        返回 (是否成功, 延迟ms, None)
        """
        start = time.time()
        sock = None
        try:
            sock = socket.create_connection((proxy_ip, proxy_port), timeout=total_timeout)
            request = (
                f"CONNECT api.ipify.org:443 HTTP/1.1\r\n"
                f"Host: api.ipify.org:443\r\n"
                f"User-Agent: {self.headers['User-Agent']}\r\n\r\n"
            )
            sock.sendall(request.encode())
            deadline = start + total_timeout
            chunks = []
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    return False, 0, None
                sock.settimeout(remain)
                data = sock.recv(4096)
                if not data:
                    return False, 0, None
                chunks.append(data)
                text = b"".join(chunks).decode("utf-8", "replace")
                head = text.split("\r\n", 1)[0]
                if " 200 " in head:
                    return True, int((time.time() - start) * 1000), None
                if "\r\n\r\n" in text:
                    return False, 0, None
        except Exception:
            return False, 0, None
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _check_http(self, proxy: str, fast_timeout: float):
        """快筛：HTTP GET 与 HTTPS CONNECT 双通道，任一通过即候选。
        - 通道1(原生HTTP GET)通过 → caps=["http"]
        - 通道2(原生CONNECT)通过 → caps=["https-pending"]，需 HTTPS 复核真实验证
        - socks 走 requests（两条通道均为真实请求，通道2直接算 https）"""
        parsed = self.parse_proxy(proxy)
        if not parsed:
            return None
        scheme, host, port = parsed
        if scheme.startswith("socks"):
            if not HAS_SOCKS:
                return None
            proxy_url = f"{scheme}://{host}:{port}"
            ok, latency, exit_ip = self._probe(proxy_url, self.http_url, fast_timeout)
            if ok:
                return {
                    "proxy": proxy, "ip": host, "port": port, "protocol": scheme,
                    "caps": ["http"], "latency_ms": latency, "exit_ip": exit_ip,
                }
            ok, latency, exit_ip = self._probe(proxy_url, self.https_url, fast_timeout)
            if ok:
                return {
                    "proxy": proxy, "ip": host, "port": port, "protocol": scheme,
                    "caps": ["https"], "latency_ms": latency, "exit_ip": exit_ip,
                }
            return None
        ok, latency, exit_ip = self._http_probe_raw(host, port, fast_timeout)
        if ok:
            return {
                "proxy": proxy, "ip": host, "port": port, "protocol": scheme,
                "caps": ["http"], "latency_ms": latency, "exit_ip": exit_ip,
            }
        ok, latency, _ = self._http_connect_raw(host, port, fast_timeout)
        if ok:
            return {
                "proxy": proxy, "ip": host, "port": port, "protocol": scheme,
                "caps": ["https-pending"], "latency_ms": latency, "exit_ip": None,
            }
        return None

    def _check_https(self, entry: dict):
        """复核：对快筛通过的代理补充 HTTPS 能力探测（原地更新 entry）。
        对 "https-pending" 条目：复核失败则返回 None（假 200 代理剔除）。"""
        parsed = self.parse_proxy(entry["proxy"])
        if not parsed:
            return entry
        scheme, host, port = parsed
        proxy_url = f"{scheme}://{host}:{port}"
        ok, latency, exit_ip = self._probe(proxy_url, self.https_url, self.timeout)
        if ok:
            if "https-pending" in entry["caps"]:
                entry["caps"].remove("https-pending")
            if "https" not in entry["caps"]:
                entry["caps"].append("https")
            if latency < entry["latency_ms"]:
                entry["latency_ms"] = latency
            if exit_ip:
                entry["exit_ip"] = exit_ip
            return entry
        if "https-pending" in entry["caps"]:
            return None
        return entry

    def _sweep(self, proxies, thread_count: int, fast_timeout: float):
        """第一段：HTTP 快速筛选（短超时，快速淘汰死代理）"""
        print(f"⚡ 快筛(HTTP)：{len(proxies)} 个 | 线程={thread_count} | 单代理超时={fast_timeout}s")
        results = []
        start = time.time()
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(self._check_http, p, fast_timeout) for p in proxies]
            for idx, future in enumerate(as_completed(futures), 1):
                detail = future.result()
                if detail:
                    results.append(detail)
                if idx % 2000 == 0:
                    print(f"   快筛进度 {idx}/{len(proxies)} | 已通过 {len(results)}")
        print(f"⚡ 快筛完成：通过 {len(results)}/{len(proxies)} 个 | 耗时 {int(time.time() - start)}s")
        return results

    def _enrich(self, entries, thread_count: int):
        """第二段：对通过快筛的代理复核 HTTPS 能力（假 200 代理在此剔除）"""
        if not entries:
            return
        print(f"⚡ HTTPS 复核：{len(entries)} 个 | 线程={thread_count} | 单代理超时={self.timeout}s")
        start = time.time()
        kept = []
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(self._check_https, e) for e in entries]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    kept.append(result)
        entries[:] = kept
        https_ok = sum(1 for e in entries if "https" in e["caps"])
        print(f"⚡ HTTPS 复核完成：支持 HTTPS {https_ok} 个 | 耗时 {int(time.time() - start)}s")

    def validate(self, proxies, thread_count: int = 500, fast_timeout: float = 0):
        """
        批量验证代理IP（多线程）
        :param proxies: 待验证代理列表（IP:PORT 或 socks5://IP:PORT）
        :param thread_count: 并发线程数
        :param fast_timeout: >0 时启用两段式：先用 HTTP 短超时快筛，通过的再补 HTTPS 复核
        :return: 有效代理详情列表 [{proxy, ip, port, protocol, caps, latency_ms, exit_ip}]
        """
        if not proxies:
            print("⚠️ 无待验证的代理IP")
            return []
        if not HAS_SOCKS:
            print("⚠️ 未安装 PySocks，socks4/socks5 代理将被跳过（pip install pysocks）")

        if fast_timeout and fast_timeout > 0:
            results = self._sweep(proxies, thread_count, fast_timeout)
            self._enrich(results, min(max(200, thread_count // 4), max(len(results), 1)))
            print(f"✅ 验证完成 | 有效代理：{len(results)} 个（总待验证：{len(proxies)} 个）")
            return results

        print(f"🔍 开始批量验证：{len(proxies)} 个代理 | 线程数={thread_count} | 单代理超时={self.timeout}s")
        results = []
        start = time.time()
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(self.check_proxy, p) for p in proxies]
            for idx, future in enumerate(as_completed(futures), 1):
                detail = future.result()
                if detail:
                    results.append(detail)
                if idx % 1000 == 0:
                    print(f"   进度 {idx}/{len(proxies)} | 已通过 {len(results)}")
        print(f"✅ 验证完成 | 有效代理：{len(results)} 个（总待验证：{len(proxies)} 个）| 耗时 {int(time.time() - start)}s")
        return results
