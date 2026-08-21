"""Google Street View thumbnail URL generation and isolated downloads."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests


class StreetViewClient:
    ENDPOINT = "https://streetviewpixels-pa.googleapis.com/v1/thumbnail"
    DIRECTIONS = (("前", 0), ("右", 90), ("后", 180), ("左", 270))

    def __init__(self, proxy: str | None = None, timeout: float = 15.0):
        self.proxy = proxy
        self.timeout = timeout

    def image_urls(self, pano_id: str, width: int = 640, height: int = 480) -> dict[str, str]:
        if not pano_id:
            raise ValueError("Pano ID 不能为空")
        return {
            name: f"{self.ENDPOINT}?{urlencode({'panoid': pano_id, 'cb_client': 'maps_sv.tactile.gps', 'w': width, 'h': height, 'pitch': 0, 'thumbfov': 100, 'yaw': yaw})}"
            for name, yaw in self.DIRECTIONS
        }

    def _request(self, url: str) -> bytes:
        kwargs = {
            "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://maps.google.com/"},
            "timeout": self.timeout,
        }
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(url, **kwargs)
                response.raise_for_status()
                if not response.content:
                    raise RuntimeError("街景服务返回空响应")
                return response.content
            except (requests.RequestException, RuntimeError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep((attempt + 1) * 1.5)
        raise RuntimeError(f"街景下载失败: {last_error}")

    def download_one(self, url: str) -> bytes:
        return self._request(url)

    def download_many(self, urls: list[str], workers: int = 4) -> list[bytes | None]:
        results: list[bytes | None] = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=min(workers, len(urls) or 1)) as pool:
            futures = {pool.submit(self._request, url): index for index, url in enumerate(urls)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:
                    print(f"  街景下载失败: {error}")
        return results
