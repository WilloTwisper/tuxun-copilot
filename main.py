# main.py
import io
import json
import logging
import os
import sys
import time
from concurrent.futures import Future
from threading import Event, Thread

from google import genai
from google.genai import types as genai_types
from PIL import Image

from config import AppConfig
from gemini_router import GeminiCapacityError, GeminiRouter
from streetview import StreetViewClient
from tuxun_client import TuxunClient
from clash_controller import ClashController, STREETVIEW_PROBE_URL

# 运行时注入，导入本模块不会读取凭证或发起网络请求。
tuxun_client: TuxunClient
streetview_client: StreetViewClient
gemini_router: GeminiRouter
clash_controller: ClashController | None
app_config: AppConfig
logger = logging.getLogger("tuxun_copilot")


def clock() -> str:
    return time.strftime("%H:%M:%S")


def debug(message: str, *args) -> None:
    if app_config.debug_log:
        logger.debug(message, *args)


def run_in_background(function, *args) -> Future:
    future: Future = Future()

    def target() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args))
        except BaseException as error:
            future.set_exception(error)

    Thread(target=target, name="round-analysis", daemon=True).start()
    return future


def initialize() -> None:
    """加载配置、构造客户端并验证图寻登录状态。"""
    global app_config, tuxun_client, streetview_client, gemini_router, clash_controller
    app_config = AppConfig.from_env()
    if app_config.proxy:
        os.environ["HTTP_PROXY"] = app_config.proxy
        os.environ["HTTPS_PROXY"] = app_config.proxy
    clients = [
        genai.Client(api_key=key, http_options=genai_types.HttpOptions(timeout=app_config.gemini_timeout_ms))
        for key in app_config.gemini_keys
    ]
    gemini_router = GeminiRouter(clients, app_config.gemini_models)
    tuxun_client = TuxunClient(app_config.tuxun_cookie, app_config.request_timeout)
    streetview_client = StreetViewClient(app_config.proxy, app_config.request_timeout)
    if app_config.debug_log:
        log_path = os.path.abspath(app_config.debug_log)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        logging.basicConfig(filename=log_path, level=logging.DEBUG, encoding="utf-8")
    clash_controller = (
        ClashController(app_config.clash_controller, app_config.clash_secret, proxy_port=app_config.proxy)
        if app_config.clash_controller else None
    )
    if not tuxun_client.current_user_id():
        raise RuntimeError("图寻 Cookie 无效或网络不可用。")

# 期望的 AI 结构化输出格式
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "continent": {"type": "string"},
        "country": {"type": "string"},
        "country_in_continent": {"type": "string"},
        "province": {"type": "string"},
        "province_in_country": {"type": "string"},
        "city": {"type": "string"},
        "city_in_province": {"type": "string"},
        "lat": {"type": "number"},
        "lng": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["continent", "country", "lat", "lng", "reasoning"],
}

ANALYSIS_PROMPT_COMMON = """你是一位顶级的图寻（GeoGuessr）专家和地理学家。
你将会收到四张从同一点拍摄、分别朝向四个不同方向（前、右、后、左）的街景图片。
请综合所有可见信息（语言、车牌、道路标识、植被、建筑风格、地形地貌、天气等），
判断最可能的位置，并严格按 JSON 字段输出：

- continent: 最可能的大洲
- country: 最可能的国家
- country_in_continent: 该国在所属大洲的大致方位，如"欧洲南部"
- province: 最可能的省份/州，不确定填"未知"
- province_in_country: 省份/州在所属国家的大致方位，不确定填"未知"
- city: 最可能的城市，不确定填"未知"
- city_in_province: 城市在所属省份/州的大致方位，不确定填"未知"
- lat / lng: 推测的经纬度（十进制小数，南纬西经用负数）
- confidence: 0 到 100 的整数，表示对总体位置判断的把握程度
- reasoning: 简短的中文分析，说明依据了图片中的哪些关键线索

请诚实回答，不要编造确定信息。"""

# 快速方案（PVP 对战）：reasoning 压缩到一句话 ≤50 字，换取更快响应。
# 基准测试：输出 419 字→28.6s，164 字→16.1s，约快 45%。
ANALYSIS_PROMPT_FAST = ANALYSIS_PROMPT_COMMON + "\n\nreasoning 务必精简：一句话，不超过 50 字，不要长篇大论。"

# 高精度方案（每日挑战）：每轮有 3 分钟时间，时间充裕，让模型给出详细的多行线索分析
ANALYSIS_PROMPT_PRECISE = ANALYSIS_PROMPT_COMMON + "\n\nreasoning 要求：逐条列出依据的关键线索（语言、车牌、植被、地形、建筑风格等）的详细中文分析，信息越全越好。"


def call_gemini(contents: list, config, max_attempts: int | None = None) -> str:
    """通过可测试的路由器调用 Gemini。"""
    return gemini_router.generate(contents, config, max_attempts=max_attempts)


def analyze_images_from_urls(image_urls: dict[str, str], analysis_mode: str, deadline_ms: int | None = None, cancel_event: Event | None = None) -> dict:
    """并行下载四个方向的街景图片，并让 Gemini 输出结构化的位置分析。"""
    # 每日挑战选择高精度方案（时间充裕），对战/其余用快速方案（输出精简、响应快）
    if analysis_mode == "precise":
        prompt_text, scheme = ANALYSIS_PROMPT_PRECISE, "高精度"
    else:
        prompt_text, scheme = ANALYSIS_PROMPT_FAST, "快速"
    debug("analysis scheme=%s urls=%s", scheme, image_urls)
    prompt_parts = [prompt_text]
    if deadline_ms is not None and time.time() * 1000 >= deadline_ms:
        raise RoundExpiredError("轮次已结束，取消图片分析")
    t_dl = time.time()
    contents = streetview_client.download_many(
        list(image_urls.values()), cancel_event=cancel_event, deadline_ms=deadline_ms
    )
    dl_elapsed = time.time() - t_dl
    # 下载偏慢（>8s）时尝试自动切节点：大概率是当前线路质量问题
    if dl_elapsed > 8 and clash_controller and analysis_mode != "fast" and (deadline_ms is None or time.time() * 1000 < deadline_ms):
        print(f"提示：下载耗时 {dl_elapsed:.1f}s 偏慢，尝试自动切换节点...")
        # 当前节点实际下载慢，拉黑一小段时间，避免测速又选回它
        clash_controller.blacklist_nodes(clash_controller.current_target_nodes(), cooldown=120)
        clash_controller.pick_and_switch()
    loaded = 0
    for direction, content in zip(image_urls.keys(), contents):
        if content is None:
            debug("image failed direction=%s", direction)
            continue
        try:
            prompt_parts.append(f"\n--- {direction}视图 ---")
            prompt_parts.append(Image.open(io.BytesIO(content)))
            loaded += 1
        except Exception as e:
            debug("image decode failed direction=%s error=%s", direction, e)

    if loaded == 0:
        if cancel_event and cancel_event.is_set():
            raise RoundExpiredError("轮次任务已取消")
        # 全部图片下载失败：当前节点对 Google 实际不通，拉黑并让上层切节点重试
        if clash_controller:
            clash_controller.blacklist_nodes(clash_controller.current_target_nodes())
        raise RuntimeError("所有方向的图片均下载失败，无法进行分析。")

    debug("images loaded=%s/4 elapsed=%.1fs", loaded, dl_elapsed)

    gemini_started = time.monotonic()
    # Gemini 调用通过代理偶发 SSL EOF / 连接中断，需重试；失败时自动切换 Clash 节点
    text = None
    last_err = None
    max_attempts = 2 if analysis_mode == "fast" else 3
    for attempt in range(1, max_attempts + 1):
        if cancel_event and cancel_event.is_set():
            raise RoundExpiredError("轮次任务已取消")
        if deadline_ms is not None and time.time() * 1000 >= deadline_ms:
            raise RoundExpiredError("轮次已结束，取消 Gemini 请求")
        try:
            text = call_gemini(prompt_parts, genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESULT_SCHEMA,
            ), max_attempts=3 if analysis_mode == "fast" else None)
            break
        except Exception as e:
            last_err = e
            is_key_err = isinstance(e, GeminiCapacityError)
            # Key 配额问题（429）：重试无益，给分钟配额窗口留足时间
            wait = 0 if is_key_err else attempt
            msg = f"Gemini 请求失败（第 {attempt} 次）: {type(e).__name__}: {e}"
            # 网络类失败（断连/SSL EOF）说明当前节点对 Google 不可靠：拉黑当前节点再切换
            is_net_err = (
                isinstance(e, (ConnectionError, TimeoutError))
                or any(k in type(e).__name__ for k in ("ReadError", "ConnectError", "SSLError", "RemoteDisconnected"))
            )
            if clash_controller and is_net_err and not is_key_err and (deadline_ms is None or time.time() * 1000 + 3_000 < deadline_ms):
                clash_controller.blacklist_nodes(clash_controller.current_target_nodes(), cooldown=300)
                debug("%s; switching clash node", msg)
                if analysis_mode != "fast":
                    clash_controller.pick_and_switch()
            else:
                debug("%s", msg)
            if wait:
                time.sleep(wait)
    if text is None:
        if isinstance(last_err, GeminiCapacityError):
            raise last_err
        raise RuntimeError(f"Gemini 请求多次失败: {last_err}")
    if not text:
        raise RuntimeError("模型未返回任何内容。")
    try:
        result = json.loads(text)
        result["_model"] = gemini_router.last_model
        result["_gemini_seconds"] = time.monotonic() - gemini_started
        return result
    except json.JSONDecodeError:
        debug("model returned invalid JSON: %s", text)
        raise RuntimeError("无法解析模型输出")


def print_result(result: dict) -> None:
    """格式化打印 AI 的分析结果，并附带可直接点击的地图链接。"""
    print("\n--- 分析结果 ---")

    print(f"大洲: {result.get('continent', '未知')}")

    country = result.get('country', '未知')
    country_loc = result.get('country_in_continent') or ''
    print(f"国家: {country}" + (f" ({country_loc})" if country_loc else ""))

    province = result.get('province', '未知')
    province_loc = result.get('province_in_country') or ''
    print(f"省份/州: {province}" + (f" ({province_loc})" if province_loc else ""))

    city = result.get('city', '未知')
    city_loc = result.get('city_in_province') or ''
    print(f"城市: {city}" + (f" ({city_loc})" if city_loc else ""))

    lat, lng = result.get('lat'), result.get('lng')
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        print(f"经纬度: {lat:.4f}, {lng:.4f}")
        print(f"地图链接: https://www.google.com/maps/search/?api=1&query={lat:.5f},{lng:.5f}")

    confidence = result.get('confidence')
    if isinstance(confidence, (int, float)):
        print(f"把握程度: {confidence:.0f}/100")
        if confidence < 40:
            print("提示: 把握程度较低，建议结合更多线索人工判断。")

    reasoning = result.get('reasoning')
    if reasoning:
        print(f"线索分析: {reasoning}")
    if result.get("_model"):
        print(f"模型: {result['_model']}，Gemini 用时: {result.get('_gemini_seconds', 0):.1f}s，总用时: {result.get('_total_seconds', 0):.1f}s")
    print("--------------------------")


def preflight_checks() -> None:
    """启动预检：街景下载 + Gemini 接口。失败时自动切换 Clash 节点并重试一次。"""
    print("\n=== 启动预检 ===")

    # 1) 街景图片下载预检
    print("预检：测试街景图片下载...")
    ok = False
    for attempt in range(1, 3):
        try:
            t0 = time.time()
            content = streetview_client.download_one(STREETVIEW_PROBE_URL)
            print(f"  下载预检通过：{len(content or b'') // 1024}KB，{time.time() - t0:.1f}s")
            ok = True
            break
        except Exception as e:
            print(f"  下载预检失败（第 {attempt} 次）: {type(e).__name__}: {e}")
            if clash_controller:
                print("  尝试自动切换 Clash 节点...")
                clash_controller.pick_and_switch()
    if not ok:
        print("  警告：下载预检未通过，后续分析可能因网络问题失败。")

    # 2) Gemini 接口预检
    print("预检：测试 Gemini 接口...")
    ok = False
    for attempt in range(1, 3):
        try:
            t0 = time.time()
            call_gemini("请只回复两个字：正常", genai_types.GenerateContentConfig(max_output_tokens=20))
            print(f"  Gemini 预检通过：{time.time() - t0:.1f}s")
            ok = True
            break
        except Exception as e:
            print(f"  Gemini 预检失败（第 {attempt} 次）: {type(e).__name__}: {e}")
            if clash_controller and not isinstance(e, GeminiCapacityError):
                print("  尝试自动切换 Clash 节点...")
                clash_controller.pick_and_switch()
    if not ok:
        print("  警告：Gemini 预检未通过，分析时可能失败。")

    print("=== 预检完成 ===\n")


class RoundExpiredError(RuntimeError):
    pass


def analyze_round(pano_id: str, analysis_mode: str, round_no: int = 0, deadline_ms: int | None = None, cancel_event: Event | None = None) -> dict:
    """下载当前轮次的四方向图片并分析，结果打印到控制台。

    失败时先按测速切最优节点重试；仍失败则强制轮换到不同节点再试一次。
    对战中一轮的机会不能轻易丢，但最多 3 次尝试。
    """
    def expired() -> bool:
        return deadline_ms is not None and time.time() * 1000 >= deadline_ms

    if expired():
        raise RoundExpiredError(f"第 {round_no} 轮已超过截止时间")
    image_urls = streetview_client.image_urls(pano_id)
    started = time.monotonic()
    try:
        result = analyze_images_from_urls(image_urls, analysis_mode, deadline_ms, cancel_event)
        result["_total_seconds"] = time.monotonic() - started
        return result
    except Exception as e:
        if isinstance(e, RoundExpiredError):
            raise
        if not clash_controller or analysis_mode == "fast" or (cancel_event and cancel_event.is_set()) or (deadline_ms is not None and time.time() * 1000 + 3_000 >= deadline_ms):
            raise
        if isinstance(e, GeminiCapacityError):
            # Key 配额问题：切节点无用，等一个分钟配额窗口后直接重试一次
            debug("round %s capacity failure: %s", round_no, e)
            raise
        # 第 1 次重试：按测速切最优节点
        debug("round %s failed with %s, switching node", round_no, type(e).__name__)
        clash_controller.pick_and_switch()
        try:
            return analyze_images_from_urls(image_urls, analysis_mode, deadline_ms, cancel_event)
        except Exception as e2:
            # 第 2 次重试：强制轮换到不同节点（测速"正常"但实际不通的场景）
            debug("round retry failed: %s; forcing node rotation", e2)
            clash_controller.pick_and_switch(exclude_current=True)
            try:
                return analyze_images_from_urls(image_urls, analysis_mode, deadline_ms, cancel_event)
            except Exception as e3:
                raise RuntimeError(f"轮换节点后重试仍失败: {e3}") from e2


def monitor_game(game_id: str) -> None:
    """监控模式：轮询游戏进度，每轮开始自动分析，游戏结束自动退出。

    同一局链接不变，轮询解决"何时读"的问题：检测到 rounds 出现新轮次
    且 panoId 就绪时才分析，绝不重复分析已分析过的轮次。
    """
    interval = app_config.monitor_interval
    print(f"\n进入监控模式（每 {interval:.0f} 秒轮询，Ctrl+C 返回输入）...")
    analyzed: set[int] = set()
    active_future: Future | None = None
    active_round: int | None = None
    active_deadline: int | None = None
    active_cancel: Event | None = None
    seen_ongoing = False
    try:
      while True:
        try:
            if active_future and active_future.done():
                try:
                    result = active_future.result()
                    latest = tuxun_client.get_game(game_id)
                    still_current = latest and latest.status == "ongoing" and latest.current_round == active_round
                    if still_current:
                        print_result(result)
                        analyzed.add(active_round)
                    else:
                        print(f"[{clock()}] 第 {active_round} 轮结果已过期，丢弃迟到答案。")
                except RoundExpiredError as error:
                    print(f"[{clock()}] 第 {active_round} 轮跳过：{error}")
                    analyzed.add(active_round)
                except Exception as error:
                    print(f"[{clock()}] 第 {active_round} 轮分析失败：{error}")
                active_future = None
                active_round = None
                active_deadline = None
                active_cancel = None

            try:
                data = tuxun_client.get_game(game_id)
            except Exception as error:
                print(f"警告：获取游戏数据失败：{error}，稍后重试...")
                time.sleep(interval)
                continue
            if not data:
                print("警告：获取游戏数据失败，稍后重试...")
                time.sleep(interval)
                continue

            status = data.status
            rounds = data.rounds
            total = data.total_rounds or '?'

            if active_future and active_round == data.current_round and active_round is not None:
                live_round = next((item for item in rounds if item.number == active_round), None)
                if live_round:
                    live_deadline = data.deadline_ms(live_round)
                    if live_deadline and int(time.time() * 1000) >= live_deadline:
                        print(f"[{clock()}] 第 {active_round} 轮已到截止时间，停止后续处理。")
                        if active_cancel:
                            active_cancel.set()
                        analyzed.add(active_round)
                        active_future = None
                        active_round = None
                        active_deadline = None
                        active_cancel = None

            if status != 'ongoing':
                if active_future and not active_future.done():
                    print(f"[{clock()}] 游戏已结束，取消展示第 {active_round} 轮迟到结果。")
                    if active_cancel:
                        active_cancel.set()
                    active_future = None
                    active_round = None
                    active_deadline = None
                # 直接粘贴已结束游戏时保留一次最后轮分析；进行中的游戏结束时不再启动新任务。
                if not seen_ongoing and not analyzed and rounds and not active_future:
                    round_ = rounds[-1]
                    print(f"\n[{clock()}] 游戏状态为 {status}，补分析最后一轮（第 {round_.number} 轮）...")
                    try:
                        print_result(analyze_round(round_.pano_id, data.mode, round_.number))
                    except Exception as error:
                        print(f"最后一轮分析失败：{error}")
                print("游戏已结束，返回等待输入。")
                return

            seen_ongoing = True

            # 只追踪当前轮，避免模型阻塞后把已经过期的旧轮次排队分析。
            round_ = data.active_round
            if round_ and round_.number not in analyzed and not active_future:
                n = round_.number
                deadline = data.deadline_ms(round_)
                remaining = (deadline - int(time.time() * 1000)) / 1000 if deadline else None
                if remaining is not None and remaining <= 0:
                    print(f"[{clock()}] 第 {n}/{total} 轮已过期，跳过。")
                    analyzed.add(n)
                else:
                    left = f"，剩余约 {remaining:.1f}s" if remaining is not None else ""
                    guesses = len(round_.guessed_user_ids)
                    players = data.player_count or "?"
                    print(f"\n[{clock()}] 第 {n}/{total} 轮开始分析{left}，已提交 {guesses}/{players}...")
                    active_cancel = Event()
                    active_future = run_in_background(analyze_round, round_.pano_id, data.mode, n, deadline, active_cancel)
                    active_round = n
                    active_deadline = deadline

            if active_future and active_round is not None and data.current_round is not None and data.current_round != active_round:
                print(f"[{clock()}] 已进入第 {data.current_round} 轮，停止展示第 {active_round} 轮后续结果。")
                if active_cancel:
                    active_cancel.set()
                analyzed.add(active_round)
                active_future = None
                active_round = None
                active_deadline = None
                active_cancel = None

            time.sleep(interval)
        except KeyboardInterrupt:
            if active_cancel:
                active_cancel.set()
            print("\n已退出监控模式，返回等待输入。")
            return
        except Exception as error:
            print(f"[{clock()}] 监控循环异常：{error}")
            time.sleep(interval)
    finally:
        if active_cancel:
            active_cancel.set()


def main() -> int:
    try:
        initialize()
    except Exception as error:
        print(f"错误：初始化失败：{error}")
        return 1

    print(f"\n--- Tuxun Copilot 已启动 (模型: {' / '.join(app_config.gemini_models)}) ---")
    try:
        # 启动预检：确认下载与 Gemini 链路可用
        preflight_checks()

        while True:
            user_input = input("\n请输入图寻的游戏ID或游戏链接 (输入 'q' 退出): ").strip()
            if user_input.lower() in ("q", "quit", "exit"):
                break
            if not user_input:
                continue

            try:
                game_id = TuxunClient.parse_game_id(user_input)
            except ValueError:
                print("输入无效：无法识别游戏ID。请粘贴纯ID或完整的游戏链接。")
                continue

            # 监控模式：自动跟随轮次分析，游戏结束自动回到这里
            monitor_game(game_id)
    except (KeyboardInterrupt, EOFError):
        print("\n收到中断信号，程序已退出。")

    print("程序已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
