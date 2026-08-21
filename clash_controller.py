# clash_controller.py
"""Clash 控制 API 封装：节点测速 + 自动切换。

配合 Clash Verge / mihomo 的 external-controller 使用（.env 配置
CLASH_CONTROLLER / CLASH_SECRET）。控制 API 只走本机回环，不经代理。

流程：
1. 解析目标域名在规则中的路由（如生成式语言 API 走"💻 AI"组、街景走谷歌组），
   沿着 Selector 链找到最深处可切换的选择组；
2. 并行测速该组内的真实节点（跳过内置 DIRECT/REJECT 与嵌套组）；
3. 把选择组切换到延迟最低的健康节点。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

_NODE_TYPES = {"Hysteria2", "Vless", "Shadowsocks", "Trojan", "ss", "vmess", "tuic", "wireguard", "Socks", "Http"}
_SKIP_MEMBERS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}
_RULE_TYPES = {"Domain", "DomainSuffix", "DomainKeyword"}
# 测速探针用真实街景缩略图：小包 204 只测延迟，无法反映下载吞吐。
# 节点可能延迟漂亮但带宽烂（实测 86ms 节点下载 4 图要 21s；x0.3 限速节点 Gemini 卡 18s）。
# 探针文件越大，mihomo 测速结果越接近真实吞吐（1280x960 ≈ 130KB）。
STREETVIEW_PROBE_URL = (
    "https://streetviewpixels-pa.googleapis.com/v1/thumbnail"
    "?panoid=XTp36Vw7VVgWxWpE3HHJfQ&cb_client=maps_sv.tactile.gps"
    "&w=1280&h=960&pitch=0&thumbfov=100&yaw=0"
)
# Gemini 组的真实探针：无 Key 请求会 401，但 TLS 握手已完成 —— 能验证
# "mihomo 测速正常但 Python/OpenSSL 连不通"的节点（返回非网络异常即通）。
GEMINI_PROBE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DOMAIN_PROBE_MAP = {
    "streetviewpixels-pa.googleapis.com": STREETVIEW_PROBE_URL,
    "generativelanguage.googleapis.com": GEMINI_PROBE_URL,
}


class ClashController:
    def __init__(self, controller: str, secret: str = "", proxy_port: str = ""):
        self.base = controller.rstrip("/")
        self.secret = secret
        # Clash 混合端口（http://127.0.0.1:7897）：真实探针请求从这里走规则路由
        self.proxy_port = proxy_port
        import requests as _requests
        self.session = _requests.Session()
        # 控制 API 是本机服务，不走代理也不信任环境变量
        self.session.trust_env = False
        self.session.proxies = {"http": None, "https": None}
        if secret:
            self.session.headers["Authorization"] = f"Bearer {secret}"
        # 真实请求失败过的节点黑名单：node -> 解禁时间戳
        # mihomo 测速走自己的 TLS 栈，对某些节点"测速正常但 Python 实际不通"，
        # 必须靠真实失败反馈把它拉黑，避免每次又被测速选回来。
        self._blacklist: dict[str, float] = {}

    # ---------- 基础请求 ----------

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None):
        import requests as _requests
        try:
            r = self.session.request(
                method, f"{self.base}{path}",
                params=params or {}, json=json_body, timeout=5,
            )
            r.raise_for_status()
            return r.json() if r.content else {}
        except _requests.exceptions.RequestException:
            return {}

    def get_proxies(self) -> dict:
        """全部代理（含组）信息。"""
        return self._request("GET", "/proxies").get("proxies") or {}

    def get_rules(self) -> list:
        return self._request("GET", "/rules").get("rules") or []

    # ---------- 路由解析 ----------

    def _rule_matches(self, rule: dict, domain: str) -> bool:
        rtype, payload = rule.get("type", ""), rule.get("payload", "")
        if rtype == "Domain":
            return payload == domain
        if rtype == "DomainSuffix":
            return domain == payload or domain.endswith("." + payload)
        if rtype == "DomainKeyword":
            return payload in domain
        return False

    def _deepest_selector(self, group_name: str, proxies: dict) -> str | None:
        """沿 Selector 链（组套组）下钻，返回最深处可手动切换的组名。

        URLTest/Fallback 组不能手动写成员，遇到时停在它们上一层。
        """
        name = group_name
        deepest = None
        for _ in range(20):
            info = proxies.get(name)
            if not info:
                break
            if info.get("type") == "Selector":
                deepest = name
                name = info.get("now") or ""
                continue
            break
        return deepest

    def resolve_targets(self, domains: list[str]) -> list[str]:
        """返回规则命中这些域名后、真正可手动切换的选择组名集合。"""
        return [g for g, _ in self._group_probe_pairs(domains)]

    def _group_probe_pairs(self, domains: list[str] | None = None) -> list[tuple[str, str]]:
        """每个目标组 -> 走该组的真实探针 URL。

        mihomo 测速（/proxies/{node}/delay）用自己的 TLS 栈且只测延迟，
        测不出"Python 实际连不通"和带宽限制；所以每个组还配一个真实
        Python 请求探针：街景组用缩略图（验吞吐），Gemini 组用 models
        端点（验 TLS，无 Key 也会 401 但握手已完成）。
        """
        pairs: dict[str, str] = {}
        proxies = self.get_proxies()
        rules = self.get_rules()
        for domain, url in _DOMAIN_PROBE_MAP.items():
            if domains and domain not in domains:
                continue
            hit = next((r for r in rules if r.get("type") in _RULE_TYPES and self._rule_matches(r, domain)), None)
            if not hit:
                print(f"警告：找不到 {domain} 的路由规则")
                continue
            deepest = self._deepest_selector(hit["proxy"], proxies)
            if deepest:
                pairs.setdefault(deepest, url)
        return list(pairs.items())

    # ---------- 测速与切换 ----------

    def node_delay(self, node: str, timeout_ms: int = 5000, url: str = STREETVIEW_PROBE_URL) -> int | None:
        """测试单个节点抓取真实街景缩略图的耗时（毫秒），失败返回 None。"""
        data = self._request("GET", f"/proxies/{quote(node)}/delay", {
            "url": url,
            "timeout": timeout_ms,
        })
        return data.get("delay")

    def switch_to(self, group: str, node: str) -> bool:
        """切换选择组到指定节点。PUT 成功时响应体为空（204），以状态码判断。"""
        import requests as _requests
        try:
            r = self.session.request(
                "PUT", f"{self.base}/proxies/{quote(group)}",
                json={"name": node}, timeout=5,
            )
            return r.status_code in (200, 204)
        except _requests.exceptions.RequestException:
            return False

    def _is_blacklisted(self, node: str) -> bool:
        """节点是否在黑名单冷却期内。"""
        exp = self._blacklist.get(node)
        if exp and exp > time.time():
            return True
        self._blacklist.pop(node, None)
        return False

    def blacklist_nodes(self, nodes: list[str], cooldown: int = 300) -> None:
        """把真实请求失败过的节点拉黑一段时间，避免测速又选回它。"""
        exp = time.time() + cooldown
        for n in nodes:
            if n:
                self._blacklist[n] = exp
                print(f"  已拉黑节点 {cooldown}s: {n}")

    def current_target_nodes(self) -> list[str]:
        """当前两组（Gemini/街景）正在使用的节点名（去重）。"""
        targets = self.resolve_targets([
            "generativelanguage.googleapis.com",
            "googleapis.com",
            "gstatic.com",
            "streetviewpixels-pa.googleapis.com",
        ])
        proxies = self.get_proxies()
        nodes = []
        for g in targets:
            now = (proxies.get(g) or {}).get("now")
            if now and now not in nodes:
                nodes.append(now)
        return nodes

    def pick_and_switch(self, timeout_ms: int = 5000, exclude_current: bool = False) -> list[str]:
        """对目标组自动切换"真实请求最快"的节点；返回切换过的组名列表。

        两阶段探测：
        1. mihomo 延迟测试并行粗筛（快，但不信它的绝对数值）；
        2. 前 5 名逐个把组切过去，用 Python 真实请求实测（街景组测吞吐、
           Gemini 组测 TLS），取真实最快者。测速说谎的节点在此被淘汰。
        """
        proxies = self.get_proxies()
        switched = []
        for group, probe_url in self._group_probe_pairs():
            info = proxies.get(group) or {}
            if info.get("type") != "Selector":
                continue
            now = info.get("now") or ""
            # 并发测速组内真实节点（跳过内置 DIRECT/REJECT、嵌套组与黑名单节点）
            leaf_nodes = [
                m for m in info.get("all") or []
                if m not in _SKIP_MEMBERS
                and proxies.get(m, {}).get("type") in _NODE_TYPES
                and not self._is_blacklisted(m)
            ]
            if not leaf_nodes:
                continue
            with ThreadPoolExecutor(max_workers=min(8, len(leaf_nodes))) as pool:
                delays = list(pool.map(lambda n: self.node_delay(n, timeout_ms, probe_url), leaf_nodes))
            candidates = [
                (n, d) for n, d in zip(leaf_nodes, delays)
                if d is not None and not (exclude_current and n == now)
            ]
            candidates.sort(key=lambda x: x[1])
            # 真实 Python 探测前 5 名：mihomo 数值只用来粗筛，真实请求才算数
            verified = []
            for node, _ in candidates[:5]:
                d = self._real_probe(group, node, probe_url, self.proxy_port)
                if d is not None:
                    verified.append((node, d))
            if not verified:
                if exclude_current:
                    print(f"{group} 轮换失败：排除当前节点后没有可用的其他节点")
                else:
                    print(f"{group} 探测全部失败（{len(leaf_nodes)} 个节点均无响应，代理链路可能暂时故障）")
                continue
            best_node, best_delay = min(verified, key=lambda x: x[1])
            if best_node != now:
                if self.switch_to(group, best_node):
                    tag = "（强制轮换）" if exclude_current else f"（{best_delay}ms）"
                    print(f"已切换 {group} => {best_node} {tag}")
                    switched.append(group)
            else:
                print(f"{group} 保持当前节点（{now}）")
        return switched

    def _real_probe(self, group: str, node: str, probe_url: str, port: str, timeout: float = 8.0) -> int | None:
        """把组切到 node，用 Python 真实请求探针 URL，返回耗时 ms。

        真实走 Clash 混合端口（规则路由到刚切换的节点），TLS 栈与业务下载完全一致。
        请求发出即算连通（Gemini 探针无 Key 会 401，但 TLS 已握手成功）。
        """
        import requests as _requests
        if not self.switch_to(group, node):
            return None
        proxies = {"http": port, "https": port}
        t0 = time.monotonic()
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if probe_url == STREETVIEW_PROBE_URL:
                headers["Referer"] = "https://maps.google.com/"
            r = _requests.get(probe_url, timeout=timeout, proxies=proxies, headers=headers)
            if probe_url == STREETVIEW_PROBE_URL:
                if r.status_code != 200 or not r.content or "image" not in r.headers.get("Content-Type", ""):
                    return None
            elif r.status_code >= 500:
                return None
            return int((time.monotonic() - t0) * 1000)
        except _requests.exceptions.RequestException:
            return None
