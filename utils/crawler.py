import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup


class ProxyCrawler:
    """代理IP爬虫类"""

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
        }
        self.proxies = []  # 存储爬取的代理IP

    def fetch(self, url: str, timeout: int = 5, post_data: Optional[Dict] = None,
              retries: int = 1) -> str:
        """请求页面，返回源码（支持GET/POST，支持重试）"""
        last_err = ""
        for attempt in range(max(1, retries)):
            try:
                if post_data:
                    response = requests.post(url, headers=self.headers, data=post_data, timeout=timeout)
                else:
                    response = requests.get(url, headers=self.headers, timeout=timeout)
                text = response.text
                if "CDN节点请求源服务器超时" in text[:800]:
                    raise RuntimeError("CDN origin timeout")
                return text
            except Exception as e:
                last_err = str(e)[:50]
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        print(f"❌ [请求失败] {url}：{last_err}")
        return ""

    @staticmethod
    def parse_api1(html: str) -> List[str]:
        """解析API响应，提取IP:PORT格式"""
        return re.findall(r'(\d+\.\d+\.\d+\.\d+:\d+)', html)

    @staticmethod
    def parse_api2(html: str) -> List[str]:
        """解析JSON接口，提取protocol=1/2的IP+Port"""
        result = []
        try:
            data = json.loads(html)
            proxy_list = []
            # 兼容多种JSON数据结构：data.data.list、data.list、直接是列表
            if isinstance(data.get("data"), dict):
                proxy_list = data["data"].get("list", [])
            elif isinstance(data, list):
                proxy_list = data
            elif isinstance(data.get("list"), list):
                proxy_list = data["list"]

            if not proxy_list:
                print("⚠️ [JSON接口] 无代理数据")
                return result

            valid_protocol = {1, 2}
            for proxy in proxy_list:
                protocol = proxy.get("protocol")
                ip = proxy.get("ip")
                port = proxy.get("port")
                protocol_valid = protocol in valid_protocol
                if protocol_valid and ip is not None and port is not None:
                    result.append(f"{str(ip).strip()}:{str(port).strip()}")

            return result
        except json.JSONDecodeError:
            print("❌ [JSON解析] 非有效JSON格式")
            return result
        except Exception as e:
            print(f"❌ [JSON解析] 异常：{str(e)[:50]}")
            return result

    @staticmethod
    def parse_article1(html: str) -> List[str]:
        """解析文章页面，提取IP:PORT格式"""
        return re.findall(r'(\d+\.\d+\.\d+\.\d+:\d+)', html)

    @staticmethod
    def parse_html1(html: str) -> List[str]:
        """解析HTML，IP+端口在同一个td标签"""
        soup = BeautifulSoup(html, 'html5lib')
        trs = soup.find_all('tr')
        return [tr.find_all('td')[0].text.strip() for tr in trs[1:]]

    @staticmethod
    def parse_html2(html: str) -> List[str]:
        """解析HTML，IP在第1个td，端口在第2个td"""
        soup = BeautifulSoup(html, 'html5lib')
        trs = soup.find_all('tr')
        return [f"{tr.find_all('td')[0].text.strip()}:{tr.find_all('td')[1].text.strip()}"
                for tr in trs[1:]]

    @staticmethod
    def parse_html3(html: str) -> List[str]:
        """解析HTML，IP在第2个td，端口在第3个td"""
        soup = BeautifulSoup(html, 'html5lib')
        trs = soup.find_all('tr')
        return [f"{tr.find_all('td')[1].text.strip()}:{tr.find_all('td')[2].text.strip()}"
                for tr in trs[1:]]

    @staticmethod
    def parse_html4(html: str) -> List[str]:
        """解析HTML，IP在第1个th，端口在第2个th"""
        soup = BeautifulSoup(html, 'html5lib')
        trs = soup.find_all('tr')
        return [f"{tr.find_all('th')[0].text.strip()}:{tr.find_all('th')[1].text.strip()}"
                for tr in trs[1:]]

    @staticmethod
    def parse_fpslist(html: str) -> List[str]:
        """解析fpsList格式JSON，提取ip+port"""
        pattern = r'const fpsList = (\[.*?\]);'
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            print("⚠️ [fpsList解析] 未找到数据")
            return []

        try:
            proxy_list = json.loads(match.group(1))
            result = []
            for item in proxy_list:
                if "ip" in item and "port" in item:
                    result.append(f"{item['ip']}:{item['port']}")

            return result
        except json.JSONDecodeError as e:
            print(f"❌ [fpsList解析] JSON错误：{str(e)[:50]}")
            return []

    @staticmethod
    def parse_fineproxy(html: str) -> List[str]:
        """解析FineProxy的响应数据, 提取ip+port"""
        try:
            response_json = json.loads(html)
        except json.JSONDecodeError as e:
            print(f"❌ [FineProxy解析] JSON错误：{str(e)[:50]}")
            return []

        rows_html = response_json.get("data", {}).get("rows", "").strip()
        if not rows_html:
            print("⚠️ [FineProxy解析] 无有效数据（rows为空）")
            return []

        pattern = r'<td\s+class=["\']table-ip["\']\s*>\s*(\d+\.\d+\.\d+\.\d+)\s*</td>\s*<td\s*>\s*(\d+)\s*</td>'
        matches = re.findall(pattern, rows_html, re.IGNORECASE | re.DOTALL)
        if not matches:
            print("⚠️ [FineProxy解析] 未匹配到IP和端口")
            return []

        result = []
        for ip, port in matches:
            port_stripped = port.strip()
            if port_stripped:
                result.append(f"{ip}:{port_stripped}")

        return result

    def _get_auto_page_count(self, url: str) -> int:
        """内部方法：自动获取api2总页数"""
        first_page_url = url + "1" if url.endswith("page=") else url
        html = self.fetch(first_page_url)
        if not html:
            print("❌ [自动分页] 获取总页数失败")
            return 1
        try:
            data = json.loads(html)
            page_count = 0
            if isinstance(data.get("data"), dict):
                page_count = data["data"].get("page_count",
                                              data["data"].get("total_pages",
                                                               data["data"].get("total_page", 0)))
            page_count = page_count or data.get("page_count", 0)
            return page_count if page_count > 0 else 1
        except Exception:
            print("❌ [自动分页] 解析总页数失败")
            return 1

    @staticmethod
    def _split_scheme(proxy: str):
        """拆分可选协议前缀，返回 (scheme, ip, port)；非法返回 None"""
        scheme, rest = "http", proxy or ""
        if "://" in proxy:
            scheme, rest = proxy.split("://", 1)
            scheme = scheme.lower()
            if scheme not in ("http", "https", "socks4", "socks5"):
                return None
        if ":" not in rest:
            return None
        ip, port_str = rest.split(":", 1)

        # 1. IP合法性校验（IPv4：四段数字，每段0-255）
        ip_segments = ip.split('.')
        if len(ip_segments) != 4:
            return None
        try:
            ip_nums = [int(seg) for seg in ip_segments]
            if not all(0 <= num <= 255 for num in ip_nums):
                return None
        except ValueError:
            return None

        # 2. 端口合法性校验（1-65535，支持数字字符串）
        try:
            port = int(port_str.strip())
            if not (1 <= port <= 65535):
                return None
        except ValueError:
            return None

        return scheme, ip, port

    def _is_valid_proxy(self, proxy: str) -> bool:
        """内部方法：校验代理字符串合法性（支持 socks4:// / socks5:// 前缀）"""
        return self._split_scheme(proxy) is not None

    def crawl(self, source: dict) -> int:
        """爬取指定源的代理IP"""
        parser = getattr(self, f"parse_{source['parser']}", None)
        if not parser:
            print(f"❌ [爬虫错误] 未知解析器：{source['parser']}")
            return 0

        # 分隔符
        source_name = source['name'].center(30, ' ')
        print(f"\n{'=' * 80}")
        print(f"📥 开始爬取 | {source_name}")
        print(f"{'=' * 80}")

        temp_proxies = []
        no_data_count = 0
        before_count = len(self.proxies)
        try:
            # 处理分页配置
            if source['pages'] == "auto":
                if source['parser'] != "api2":
                    print("⚠️ [分页警告] 仅api2支持自动分页，默认爬1页")
                    total_pages = 1
                else:
                    total_pages = self._get_auto_page_count(source['url'])
                    print(f"ℹ️ [分页信息] 自动获取总页数：{total_pages} 页")
            else:
                total_pages = source['pages']
                print(f"ℹ️ [分页信息] 配置爬取页数：{total_pages} 页")

            # 分页爬取
            for current_page in range(1, total_pages + 1):
                # 构建请求URL
                if "api1" in source['parser']:
                    url = source['url']
                elif source['parser'] in ["fineproxy"]:
                    url = source['url']
                elif any(domain in source['url'] for domain in ["kxdaili.com", "qiyunip.com"]):
                    url = f"{source['url']}{current_page}.html"
                else:
                    url = f"{source['url']}{current_page}"

                # 处理POST数据
                post_data = source.get("body") or None
                if post_data and isinstance(post_data, dict):
                    post_data = {k: v.replace("{page}", str(current_page)) if "{page}" in str(v) else v
                                 for k, v in post_data.items()}

                # 分页日志
                print(f"\n🔄 正在爬取 | 页码：{current_page:2d}/{total_pages:2d} | URL：{url}")
                print(f"ℹ️  当前累计 | 总列表IP数：{len(self.proxies):3d} 个")

                html = self.fetch(url, post_data=post_data, retries=source.get("retries", 1))

                # 仅在第1页显示预览
                if current_page == 1 and html:
                    preview = html[:500].replace('\n', ' ').strip()  # 去除换行，精简显示
                    print(f"📄 响应预览：{preview}...")

                if not html:
                    print(f"⚠️  页码 {current_page:2d} | 请求失败，跳过")
                    no_data_count += 1
                else:
                    ips = parser(html)
                    valid_ips = [ip for ip in ips if self._is_valid_proxy(ip)]  # 校验结果
                    page_valid_count = len(valid_ips)

                    # 分页结果日志
                    print(f"✅  页码 {current_page:2d} | 提取IP：{len(ips):2d} 个 | 有效格式：{page_valid_count:2d} 个")

                    temp_proxies.extend(valid_ips)
                    no_data_count = 0 if page_valid_count > 0 else no_data_count + 1

                    if page_valid_count == 0:
                        print(f"⚠️  页码 {current_page:2d} | 无有效IP，连续无数据次数：{no_data_count}")

                # 连续3次无数据停止
                if no_data_count >= 3:
                    print(f"\n🛑 [爬取停止] 连续3次无有效IP，提前结束当前源爬取")
                    break

                time.sleep(source['delay'])

            # 爬取完成统计
            self.proxies.extend(temp_proxies)
            crawl_count = len(temp_proxies)
            after_count = len(self.proxies)
            print(f"\n{'=' * 80}")
            print(f"✅ 爬取完成 | {source['name']}")
            print(f"   ├─ 本源性新增有效IP：{crawl_count:3d} 个")
            print(f"   ├─ 总列表累计IP数：{after_count:3d} 个")
            print(f"   └─ 本次新增量：{after_count - before_count:3d} 个")
            print(f"{'=' * 80}")

        except Exception as e:
            print(f"\n{'=' * 80}")
            print(f"❌ 爬取失败 | {source['name']}")
            print(f"   ├─ 失败原因：{str(e)[:50]}")
            valid_temp = [ip for ip in temp_proxies if re.match(r'\d+\.\d+\.\d+\.\d+:\d+', ip)]
            crawl_count = len(valid_temp)
            if valid_temp:
                self.proxies.extend(valid_temp)
                after_count = len(self.proxies)
                print(f"   ├─ 异常恢复：已累计有效IP {crawl_count:3d} 个")
                print(f"   └─ 总列表当前累计：{after_count:3d} 个")
            print(f"{'=' * 80}")

        return crawl_count

    def crawl_source(self, source: dict) -> int:
        """按配置分发：custom 源走专用抓取器，其余走通用爬虫"""
        custom = source.get("custom")
        if custom == "proxora":
            return self._crawl_proxora(source)
        if custom == "geonode":
            return self._crawl_geonode(source)
        if custom == "scdn":
            return self._crawl_scdn(source)
        if custom == "plain":
            return self._crawl_plain(source)
        return self.crawl(source)

    def _finish_source(self, source: dict, temp_proxies: list) -> int:
        """校验并合入某源的抓取结果，打印汇总"""
        valid = [p for p in temp_proxies if self._is_valid_proxy(p)]
        self.proxies.extend(valid)
        print(f"\n{'=' * 80}")
        print(f"✅ 爬取完成 | {source['name']}")
        print(f"   ├─ 本源性新增有效IP：{len(valid):5d} 个")
        print(f"   └─ 总列表累计IP数：{len(self.proxies):5d} 个")
        print(f"{'=' * 80}")
        return len(valid)

    def _crawl_proxora(self, source: dict) -> int:
        """Proxora 官方 JSON API：https://api.proxora.io/api/tools/free-proxies"""
        source_name = source['name'].center(30, ' ')
        print(f"\n{'=' * 80}")
        print(f"📥 开始爬取 | {source_name}")
        print(f"{'=' * 80}")
        collected, page, fetched, total = [], 1, 0, None
        while True:
            url = f"{source['url']}{page}"
            html = self.fetch(url, timeout=20, retries=source.get("retries", 3))
            if not html:
                break
            try:
                data = json.loads(html)
            except json.JSONDecodeError:
                print("❌ [Proxora] 非有效JSON")
                break
            items = data.get("items") or []
            total = data.get("total") or total
            fetched += len(items)
            for it in items:
                host, port = it.get("host"), it.get("port")
                if not host or not port:
                    continue
                proto = str(it.get("protocol") or "http").lower()
                if proto == "socks5":
                    collected.append(f"socks5://{host}:{port}")
                elif proto == "socks4":
                    collected.append(f"socks4://{host}:{port}")
                else:
                    collected.append(f"{host}:{port}")
            print(f"✅ Proxora | 页码 {page:2d} | 提取 {len(items):3d} 个 | 累计 {len(collected):5d}")
            if not items or (total and fetched >= total):
                break
            page += 1
            time.sleep(source.get("delay", 1))
        return self._finish_source(source, collected)

    def _crawl_geonode(self, source: dict) -> int:
        """GeoNode JSON API：https://proxylist.geonode.com/api/proxy-list"""
        source_name = source['name'].center(30, ' ')
        print(f"\n{'=' * 80}")
        print(f"📥 开始爬取 | {source_name}")
        print(f"{'=' * 80}")
        collected, page, fetched, total = [], 1, 0, None
        while True:
            url = f"{source['url']}{page}"
            html = self.fetch(url, timeout=20, retries=source.get("retries", 3))
            if not html:
                break
            try:
                data = json.loads(html)
            except json.JSONDecodeError:
                print("❌ [GeoNode] 非有效JSON")
                break
            items = data.get("data") or []
            total = data.get("total") or total
            fetched += len(items)
            for it in items:
                host, port = it.get("ip"), it.get("port")
                if not host or not port:
                    continue
                protos = it.get("protocols") or ["http"]
                for proto in protos:
                    proto = str(proto).lower()
                    if proto == "socks5":
                        collected.append(f"socks5://{host}:{port}")
                    elif proto == "socks4":
                        collected.append(f"socks4://{host}:{port}")
                    else:
                        collected.append(f"{host}:{port}")
            print(f"✅ GeoNode | 页码 {page:2d} | 提取 {len(items):3d} 个 | 累计 {len(collected):5d}")
            if not items or (total and fetched >= total):
                break
            page += 1
            time.sleep(source.get("delay", 1))
        return self._finish_source(source, collected)

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """跳过内网/保留地址（0/10/127/192.168/172.16-31/169.254）"""
        a, b = int(ip.split('.')[0]), int(ip.split('.')[1])
        if a in (0, 10, 127):
            return True
        if a == 192 and b == 168:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 169 and b == 254:
            return True
        return False

    def _crawl_plain(self, source: dict) -> int:
        """通用文本列表源：urls 为字符串或 {"url","scheme","max_items"} 字典列表"""
        source_name = source['name'].center(30, ' ')
        print(f"\n{'=' * 80}")
        print(f"📥 开始爬取 | {source_name}")
        print(f"{'=' * 80}")
        collected, seen = [], set()
        for entry in source.get("urls", []):
            if isinstance(entry, str):
                url, default_scheme, max_items = entry, source.get("scheme", "http"), 0
            else:
                url = entry.get("url", "")
                default_scheme = str(entry.get("scheme") or source.get("scheme") or "http").lower()
                max_items = int(entry.get("max_items") or 0)
            if not url:
                continue
            html = self.fetch(url, timeout=source.get("timeout", 30),
                              retries=source.get("retries", 2))
            if not html:
                print(f"⚠️ {source['name']} | 请求失败：{url}")
                continue
            pairs = []
            for line in html.splitlines():
                m = re.search(r"(?:([A-Za-z0-9]+)://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", line)
                if not m:
                    continue
                ip, port = m.group(2), int(m.group(3))
                if not (1 <= port <= 65535) or self._is_private_ip(ip):
                    continue
                sc = (m.group(1) or default_scheme).lower()
                if sc not in ("http", "https", "socks4", "socks5"):
                    sc = default_scheme
                key = f"{sc}://{ip}:{port}" if sc in ("socks4", "socks5") else f"{ip}:{port}"
                if key not in seen:
                    seen.add(key)
                    pairs.append(key)
            if max_items and len(pairs) > max_items:
                pairs = random.sample(pairs, max_items)
            collected.extend(pairs)
            print(f"✅ {source['name']} | {url.split('/')[-1]:28s} | 提取 {len(pairs):6d} 个 | 累计 {len(collected):6d}")
            time.sleep(source.get("delay", 0.3))
        return self._finish_source(source, collected)

    def _crawl_scdn(self, source: dict) -> int:
        """SCDN：text.php 全量 + socks4/socks5 协议分页列表（按延迟自适应截断）"""
        source_name = source['name'].center(30, ' ')
        print(f"\n{'=' * 80}")
        print(f"📥 开始爬取 | {source_name}")
        print(f"{'=' * 80}")
        plain = []
        html = self.fetch(source["url"], timeout=source.get("timeout", 40),
                          retries=source.get("retries", 2))
        if html:
            plain = self.parse_api1(html)
        print(f"✅ SCDN | text.php 提取 {len(plain):5d} 个")

        socks5_pairs = self._crawl_scdn_proto(source, "socks5")
        socks4_pairs = self._crawl_scdn_proto(source, "socks4")
        socks4_only = socks4_pairs - socks5_pairs
        socks_all = socks5_pairs | socks4_only
        print(f"✅ SCDN | socks5 提取 {len(socks5_pairs):5d} 个 | socks4 独有 {len(socks4_only):5d} 个")

        collected = [p for p in plain if p not in socks_all]
        collected += [f"socks5://{p}" for p in socks5_pairs]
        collected += [f"socks4://{p}" for p in socks4_only]
        if len(plain) < 100:
            print("⚠️ SCDN text.php 不可用或数据过少，降级 get_proxies.php 分页抓取...")
            collected += [p for p in self._crawl_scdn_table(source) if p not in socks_all]
        return self._finish_source(source, collected)

    def _fetch_scdn_page(self, base: str, proto: str, page: int, per_page: int):
        """抓取 SCDN 单页，返回 (rows_html, totalPages)；失败返回 (None, None)"""
        html = self.fetch(f"{base}?protocol={proto}&page={page}&per_page={per_page}",
                          timeout=20, retries=2)
        if not html:
            return None, None
        try:
            data = json.loads(html)
        except json.JSONDecodeError:
            return None, None
        return data.get("table_html") or "", data.get("totalPages")

    @staticmethod
    def _parse_scdn_rows(rows_html: str, max_ms: int) -> list:
        """解析单页表格行，过滤超过 max_latency_ms 的条目，返回 ip:port 列表"""
        pairs = []
        for row in rows_html.split("<tr>"):
            m = re.search(r'cell-ip">([\d.]+)</td><td>(\d+)</td>', row)
            if not m:
                continue
            ms_m = re.search(r'(\d+)ms', row)
            if ms_m and int(ms_m.group(1)) > max_ms:
                continue
            pairs.append(f"{m.group(1)}:{m.group(2)}")
        return pairs

    def _crawl_scdn_proto(self, source: dict, proto: str) -> set:
        """按协议分页抓取 SCDN：第1页取总页数后并行抓取其余页（按延迟过滤 max_latency_ms）"""
        base = source.get("fallback_url", "https://proxy.scdn.io/get_proxies.php")
        per_page = 100
        max_pages = source.get("max_pages", 200)
        max_ms = source.get("max_latency_ms", 4000)
        rows_html, total_pages = self._fetch_scdn_page(base, proto, 1, per_page)
        if not rows_html:
            print(f"⚠️ SCDN {proto} 第1页获取失败")
            return set()
        pairs = set(self._parse_scdn_rows(rows_html, max_ms))

        if total_pages:
            last_page = min(int(total_pages), max_pages)
            pages = list(range(2, last_page + 1))
            workers = int(source.get("page_workers", 8))
            if pages:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [executor.submit(self._fetch_scdn_page, base, proto, p, per_page)
                               for p in pages]
                    for fut in as_completed(futures):
                        rh, _ = fut.result()
                        if rh:
                            pairs.update(self._parse_scdn_rows(rh, max_ms))
            print(f"ℹ️ SCDN {proto} 并行抓取完成：共 {1 + len(pages)} 页 / 有效 {len(pairs)} 个")
            return pairs

        page = 2
        while page <= max_pages:
            rh, _ = self._fetch_scdn_page(base, proto, page, per_page)
            if not rh:
                break
            row_pairs = self._parse_scdn_rows(rh, max_ms)
            pairs.update(row_pairs)
            if len(row_pairs) < per_page:
                break
            page += 1
            time.sleep(source.get("delay", 0.2))
        print(f"ℹ️ SCDN {proto} 顺序抓取完成：共 {page} 页 / 有效 {len(pairs)} 个")
        return pairs


    def _crawl_scdn_table(self, source: dict, max_pages: int = 200, per_page: int = 100) -> list:
        """SCDN 分页表格接口（降级方案）"""
        base = source.get("fallback_url", "https://proxy.scdn.io/get_proxies.php")
        result = []
        for page in range(1, max_pages + 1):
            html = self.fetch(f"{base}?page={page}&per_page={per_page}", timeout=20, retries=2)
            if not html:
                break
            try:
                rows_html = (json.loads(html).get("table_html") or "")
            except json.JSONDecodeError:
                break
            rows = 0
            for row in rows_html.split("<tr>"):
                m = re.search(r'cell-ip">([\d.]+)</td><td>(\d+)</td>', row)
                if m:
                    result.append(f"{m.group(1)}:{m.group(2)}")
                    rows += 1
            if rows == 0 or rows < per_page:
                break
            time.sleep(0.2)
        print(f"ℹ️ SCDN 降级抓取完成：{len(result)} 条")
        return result

    def get_unique_proxies(self) -> List[str]:
        """代理IP去重（优化日志显示）"""
        before_count = len(self.proxies)
        unique_list = list(set(self.proxies))
        after_count = len(unique_list)
        duplicate_count = before_count - after_count
        self.proxies.clear()

        # 计算重复率（处理除以0）
        duplicate_rate = (duplicate_count / before_count) * 100 if before_count != 0 else 0.0

        # 优化去重日志格式
        print(f"\n{'=' * 60}")
        print(f"🔍 代理IP去重统计")
        print(f"   ├─ 去重前总数量：{before_count:3d} 个")
        print(f"   ├─ 去重后总数量：{after_count:3d} 个")
        print(f"   ├─ 重复IP数量：{duplicate_count:3d} 个")
        print(f"   └─ 重复率：{duplicate_rate:6.2f}%")
        print(f"{'=' * 60}")

        return unique_list
