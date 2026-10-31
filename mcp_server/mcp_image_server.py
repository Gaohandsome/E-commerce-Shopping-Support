"""
需求：实现基于 MCP 的商品实拍图检索服务器（端口 8004）

为什么单独做一个图片 MCP，而不是接一个通用的“网页搜索 MCP”？
    通用网页搜索返回的是网页，图片往往混在 HTML/CSS 里，需要脆弱的正则解析，
    而且缺少标题、原图页、作者、授权信息等结构化字段。
    专用图片工具可以统一返回：
        image_url / thumb_url / page_url / site / author / license / width / height
    种草（文案）代理只要拿结构化结果拼装 Markdown，不需要理解网页结构。

检索策略（三级降级，命中即返回）：
    1. 精确级："{品牌} {商品名}"          —— 命中商品本身
    2. 名称级："{商品名}"                 —— 去掉品牌再试一次
    3. 同类级："{品类核心词} 商品实拍"     —— 示例商品多为虚构品牌，退回同类商品图，
       并在结果里用 match_level="generic" 标注，文案中会写“同类参考图”而不是“实拍”，
       避免把别家的图谎称成这个商品的实拍。

图源策略（按 IMAGE_PROVIDER_ORDER 依次尝试，命中即返回）：
    1. 有 Key 的正版图库：Unsplash / Pexels / Pixabay
    2. 开源/公共版权图库：Openverse / Wikimedia Commons
    3. 无 Key 的图片检索兜底：360 图片（中文商品命中率较好）、Bing 图片

说明：
    - 本机网络无法直连 Wikimedia / Openverse，且没有 Unsplash / Pexels / Pixabay Key，
      因此演示环境在 .env 中把顺序设为 so360,bing；换到可访问开源图库的网络后，
      把 IMAGE_PROVIDER_ORDER 改成 openverse,wikimedia 即可优先使用 CC 授权图片。
    - 360/Bing 返回图片的授权未知，工具会原样返回原图页并标注“版权归原作者”，
      仅用于课程演示；商用必须取得原作者授权。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastmcp import FastMCP

# 确保项目根目录在 sys.path 中（支持直接以脚本方式运行 python mcp_server/xxx.py）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from create_logger import logger


# ==================== 配置 ====================
def _load_dotenv() -> None:
    """轻量加载项目根目录 .env，shell 中已有的环境变量优先。"""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

REQUEST_TIMEOUT = float(os.getenv("IMAGE_REQUEST_TIMEOUT", "10"))
CACHE_TTL = int(os.getenv("IMAGE_CACHE_TTL", "21600"))  # 6 小时
FAIL_COOLDOWN = int(os.getenv("IMAGE_PROVIDER_COOLDOWN", "600"))  # 失败后 10 分钟内不再尝试

# 这些站点是“下载站/正版图库/内容农场”，缩略图常带水印或是错误内容
BLOCKED_SITE_KEYWORDS = (
    "699pic.com", "58pic.com", "zhituwang", "tukuppt", "vcg.com", "veer.com",
    "gettyimages", "shutterstock", "alamy.com", "dreamstime", "123rf",
    "tuchong.com", "500px.com", "hellorf.com", "photophoto.cn",
    "cnxiangyan", "yanpk.com", "loupan.com", "ke.com", "anjuke.com",
    "zhaopin", "51job", "liepin", "pizsalad", "weather.com.cn",
)
# 商品场景下额外过滤：新闻/招聘/房产/价格表这类明显不是商品图的标题
BLOCKED_TITLE_KEYWORDS = (
    "香烟", "烟价格", "价目表", "楼盘", "房产", "房价", "招聘", "贷款",
    "股票", "基金", "天气预报", "地图导览", "logo", "图标", "壁纸下载",
    "官网首页", "说明书下载", "驱动下载", "维修点",
)

# 可信电商/科技媒体站点轻微加权，不强制，只用于同分排序
TRUSTED_SITE_KEYWORDS = (
    "jd.com", "tmall.com", "taobao.com", "suning.com", "vip.com", "xiaomi.com",
    "zol.com.cn", "pconline.com.cn", "ithome.com", "cnbeta.com", "smzdm.com",
    "xinhuanet.com", "people.com.cn", "sina.com.cn", "sohu.com", "163.com",
    "qq.com", "ifeng.com", "myzaker.com", "cyzone.cn",
)

# 品类词 / 商品类型词：用于把“声海 X1 真无线蓝牙耳机”压缩成“蓝牙耳机”这类通用检索词
GENERIC_TERMS = (
    "真无线蓝牙耳机", "降噪蓝牙耳机", "蓝牙耳机", "头戴式耳机", "降噪耳机", "耳机",
    "快充充电宝", "移动电源", "充电宝",
    "扫地机器人", "扫拖一体机", "吸尘器",
    "空气炸锅", "电饭煲", "破壁机", "咖啡机", "电水壶",
    "机械键盘", "无线鼠标", "显示器", "摄像头", "路由器",
    "精华液", "面霜", "洁面乳", "面膜", "洗发水", "电动牙刷",
    "运动鞋", "跑步鞋", "板鞋", "双肩包", "卫衣", "T恤", "牛仔裤",
    "咖啡豆", "坚果", "牛奶", "气泡水", "零食",
    "瑜伽垫", "哑铃", "跳绳", "帐篷",
)

_CACHE: dict[tuple, tuple[float, dict]] = {}
_PROVIDER_DOWN_UNTIL: dict[str, float] = {}
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ==================== 通用工具 ====================
def _text(value, limit: int = 160) -> str:
    """去掉 HTML 标签和多余空白，截断过长的图片标题。"""
    value = re.sub(r"<[^>]+>", "", str(value or ""))
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _is_http(url: str) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _blocked(item: dict) -> bool:
    site = (item.get("site") or _host(item.get("page_url", ""))).lower()
    title = item.get("title", "").lower()
    if any(word in site for word in BLOCKED_SITE_KEYWORDS):
        return True
    if any(word in title for word in BLOCKED_TITLE_KEYWORDS):
        return True
    image_url = (item.get("image_url") or "").lower()
    if image_url.endswith(".gif") or image_url.endswith(".svg"):
        return True
    return False


def _match_level(title: str, product: str, brand: str) -> str:
    """
    按图片标题判断这张图与目标商品的关系，供文案如实标注：
        exact   —— 标题含完整商品名，或同时含品牌 + 型号（如“声海 X1”）
        name    —— 标题只含型号（如“X1”），大概率是同一款
        brand   —— 标题只含品牌，是同品牌其他型号
        generic —— 只匹配到品类，属于同类商品参考图
    这样做是为了不把“品牌的其他型号”谎称成“该商品实拍”。
    """
    title = title or ""
    if product and product in title:
        return "exact"

    # 型号 token：字母+数字组合（X1 / PX210BT / 20000mAh 里的 20000）
    model_tokens = [
        token for token in re.findall(r"[A-Za-z]+[\d][A-Za-z0-9]*|\d{3,}[A-Za-z]*", product)
        if len(token) >= 2
    ]
    has_model = any(token.lower() in title.lower() for token in model_tokens)
    has_brand = bool(brand) and brand in title

    if has_model and has_brand:
        return "exact"
    if has_model:
        return "name"
    if has_brand:
        return "brand"
    return "generic"


def _relevance(item: dict, product: str, brand: str = "", category: str = "") -> int:
    """
    给候选图打分：
        +30 标题包含完整商品名
        +20 标题包含品牌
        +8  标题包含品类核心词
        +6  标题每命中一个商品名的二字片段
        +4  来自可信电商/科技站点
        +2  方图或横图（商品图常见方图，这里不歧视竖图，仅给轻微加分）
        +1  分辨率足够
    """
    title = item.get("title", "")
    site = (item.get("site") or "").lower()
    score = 0
    if product and product in title:
        score += 30
    if brand and brand in title:
        score += 20
    if category and category in title:
        score += 8
    key = brand and product.replace(brand, "", 1).strip() or product
    shingles = {key[i:i + 2] for i in range(len(key) - 1)} if len(key) > 1 else set()
    score += 6 * min(3, sum(1 for s in shingles if s in title))
    if any(word in site for word in TRUSTED_SITE_KEYWORDS):
        score += 4
    width, height = item.get("width") or 0, item.get("height") or 0
    if width and height and width >= 400 and height >= 400:
        score += 2
    if width >= 800 and height >= 500:
        score += 1
    return score


def _clean_results(items: list[dict], product: str, brand: str, category: str,
                   count: int) -> list[dict]:
    """过滤、去重、按相关度排序，只保留真正可渲染的图片 URL。"""
    seen = set()
    cleaned = []
    for item in items:
        if not _is_http(item.get("image_url", "")):
            continue
        if _blocked(item):
            continue
        dedupe_key = item["image_url"].split("?")[0]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        item["score"] = _relevance(item, product, brand, category)
        item["match_level"] = _match_level(item.get("title", ""), product, brand)
        cleaned.append(item)

    # 至少命中品牌 / 品类 / 商品名片段，避免把新闻配图当成商品图
    key = brand and product.replace(brand, "", 1).strip() or product
    shingles = {key[i:i + 2] for i in range(len(key) - 1)} if len(key) > 1 else set()
    relevant = [
        item for item in cleaned
        if item["score"] >= 6
        or (brand and brand in item.get("title", ""))
        or (category and category in item.get("title", ""))
        or any(s in item.get("title", "") for s in shingles)
    ]
    pool = relevant or cleaned
    pool.sort(key=lambda x: (x["score"], (x.get("width") or 0) * (x.get("height") or 0)),
              reverse=True)
    return pool[:count]


def _base_item(title, image_url, page_url="", site="", author="", license_name="unknown",
               license_note="", width=0, height=0, provider="", original_url="") -> dict:
    return {
        "title": _text(title) or "商品实拍",
        "image_url": image_url,
        "original_url": original_url or image_url,
        "page_url": page_url,
        "site": site or _host(page_url) or provider,
        "author": _text(author, 60),
        "license": license_name,
        "license_note": license_note,
        "width": int(width or 0),
        "height": int(height or 0),
        "provider": provider,
    }


def _unknown_license_note() -> str:
    return "版权未知，归原作者所有；仅用于课程演示，商用请通过原图页取得授权"


def _request_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    merged = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        merged.update(headers)
    response = requests.get(url, params=params, headers=merged, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


# ==================== 有 Key 的正版图库 ====================
def _search_unsplash(query: str, product: str, brand: str, category: str,
                     count: int) -> list[dict]:
    key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        return []
    data = _request_json(
        "https://api.unsplash.com/search/photos",
        params={"query": query, "per_page": max(count * 2, 5), "orientation": "landscape",
                "content_filter": "high", "client_id": key},
    )
    results = []
    for photo in data.get("results", []):
        urls = photo.get("urls", {})
        user = photo.get("user", {})
        results.append(_base_item(
            title=photo.get("alt_description") or photo.get("description") or product,
            image_url=urls.get("regular") or urls.get("small") or "",
            page_url=(photo.get("links") or {}).get("html", ""),
            site="unsplash.com",
            author=user.get("name", ""),
            license_name="Unsplash License",
            license_note="可免费使用；再分发/商用请遵守 Unsplash License",
            width=photo.get("width", 0),
            height=photo.get("height", 0),
            provider="unsplash",
        ))
    return _clean_results(results, product, brand, category, count)


def _search_pexels(query: str, product: str, brand: str, category: str,
                   count: int) -> list[dict]:
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    data = _request_json(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": max(count * 2, 5), "orientation": "landscape"},
        headers={"Authorization": key},
    )
    results = []
    for photo in data.get("photos", []):
        src = photo.get("src", {})
        results.append(_base_item(
            title=photo.get("alt") or product,
            image_url=src.get("large") or src.get("medium") or "",
            page_url=photo.get("url", ""),
            site="pexels.com",
            author=photo.get("photographer", ""),
            license_name="Pexels License",
            license_note="可免费使用；再分发/商用请遵守 Pexels License",
            width=photo.get("width", 0),
            height=photo.get("height", 0),
            provider="pexels",
        ))
    return _clean_results(results, product, brand, category, count)


def _search_pixabay(query: str, product: str, brand: str, category: str,
                    count: int) -> list[dict]:
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    data = _request_json(
        "https://pixabay.com/api/",
        params={"key": key, "q": query, "image_type": "photo", "orientation": "horizontal",
                "per_page": max(count * 2, 5), "safesearch": "true"},
    )
    results = []
    for hit in data.get("hits", []):
        results.append(_base_item(
            title=hit.get("tags") or product,
            image_url=hit.get("largeImageURL") or hit.get("webformatURL") or "",
            page_url=hit.get("pageURL", ""),
            site="pixabay.com",
            author=hit.get("user", ""),
            license_name="Pixabay Content License",
            license_note="可免费使用；再分发/商用请遵守 Pixabay Content License",
            width=hit.get("imageWidth", 0),
            height=hit.get("imageHeight", 0),
            provider="pixabay",
        ))
    return _clean_results(results, product, brand, category, count)


# ==================== 开源/公共版权图库 ====================
def _search_openverse(query: str, product: str, brand: str, category: str,
                      count: int) -> list[dict]:
    data = _request_json(
        "https://api.openverse.org/v1/images/",
        params={"q": query, "page_size": max(count * 2, 5), "license_type": "all-cc",
                "mature": "false"},
    )
    results = []
    for item in data.get("results", []):
        license_name = item.get("license", "") + " " + item.get("license_version", "")
        results.append(_base_item(
            title=item.get("title") or product,
            image_url=item.get("thumbnail") or item.get("url") or "",
            original_url=item.get("url") or "",
            page_url=item.get("foreign_landing_url", ""),
            site=item.get("source", "openverse"),
            author=item.get("creator", ""),
            license_name=license_name.strip() or "CC",
            license_note=(
                f"CC 授权（{license_name.strip() or 'CC'}），作者：{item.get('creator') or '未知'}；"
                "使用请保留署名并遵守对应协议"
            ),
            width=item.get("width", 0),
            height=item.get("height", 0),
            provider="openverse",
        ))
    return _clean_results(results, product, brand, category, count)


def _search_wikimedia(query: str, product: str, brand: str, category: str,
                      count: int) -> list[dict]:
    data = _request_json(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "origin": "*",
            "generator": "search", "gsrnamespace": 6, "gsrlimit": max(count * 3, 8),
            "gsrsearch": f"{query} filetype:bitmap",
            "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 960,
        },
    )
    pages = ((data.get("query") or {}).get("pages") or {}).values()
    results = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        license_name = _text((meta.get("LicenseShortName") or {}).get("value"), 40) or "Wikimedia"
        artist = _text((meta.get("Artist") or {}).get("value"), 60)
        results.append(_base_item(
            title=page.get("title", "").replace("File:", "") or product,
            image_url=info.get("thumburl") or info.get("url") or "",
            original_url=info.get("url") or "",
            page_url=info.get("descriptionurl", ""),
            site="commons.wikimedia.org",
            author=artist,
            license_name=license_name,
            license_note=f"{license_name}，作者：{artist or '未知'}；使用请保留署名并遵守对应协议",
            width=info.get("thumbwidth") or info.get("width") or 0,
            height=info.get("thumbheight") or info.get("height") or 0,
            provider="wikimedia",
        ))
    return _clean_results(results, product, brand, category, count)


# ==================== 无 Key 图片检索兜底 ====================
def _search_so360(query: str, product: str, brand: str, category: str,
                  count: int) -> list[dict]:
    """360 图片搜索：中文商品命中率高，返回图床 CDN 地址，浏览器可直接加载。"""
    data = _request_json(
        "https://image.so.com/j",
        params={"q": query, "pn": 0, "rn": max(18, count * 8)},
        headers={"Referer": "https://image.so.com/"},
    )
    results = []
    for item in data.get("list", []):
        thumb = item.get("thumb") or item.get("thumb_bak") or ""
        original = item.get("img") or thumb
        page_url = item.get("link") or ""
        results.append(_base_item(
            title=item.get("title") or product,
            image_url=thumb or original,
            original_url=original,
            page_url=page_url,
            site=item.get("site") or _host(page_url),
            license_name="unknown",
            license_note=_unknown_license_note(),
            width=item.get("width", 0),
            height=item.get("height", 0),
            provider="so360",
        ))
    return _clean_results(results, product, brand, category, count)


def _search_bing(query: str, product: str, brand: str, category: str,
                 count: int) -> list[dict]:
    """Bing 图片搜索兜底：使用 Bing 缩略图 CDN，避免原站防盗链。"""
    response = requests.get(
        "https://cn.bing.com/images/search",
        params={"q": f'"{query}"'},
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = []
    for match in re.finditer(r'm="([^"]+)"', response.text):
        try:
            item = json.loads(html.unescape(match.group(1)))
        except (ValueError, json.JSONDecodeError):
            continue
        image_url = item.get("turl") or item.get("murl") or ""
        if not image_url:
            continue
        results.append(_base_item(
            title=item.get("t") or product,
            image_url=image_url,
            original_url=item.get("murl") or image_url,
            page_url=item.get("purl") or "",
            site=_host(item.get("purl") or ""),
            license_name="unknown",
            license_note=_unknown_license_note(),
            width=item.get("w", 0),
            height=item.get("h", 0),
            provider="bing",
        ))
    return _clean_results(results, product, brand, category, count)


# ==================== 图源调度 ====================
_PROVIDERS = {
    "unsplash": _search_unsplash,
    "pexels": _search_pexels,
    "pixabay": _search_pixabay,
    "openverse": _search_openverse,
    "wikimedia": _search_wikimedia,
    "so360": _search_so360,
    "bing": _search_bing,
}


def _provider_order() -> list[str]:
    configured = os.getenv("IMAGE_PROVIDER_ORDER", "").strip()
    if configured:
        return [p.strip().lower() for p in configured.split(",") if p.strip().lower() in _PROVIDERS]
    return ["unsplash", "pexels", "pixabay", "openverse", "wikimedia", "so360", "bing"]


def _generic_term(product: str, category: str) -> str:
    """从商品名里识别出通用品类词（如“声海 X1 真无线蓝牙耳机”→“真无线蓝牙耳机”）。"""
    for term in GENERIC_TERMS:
        if term in product:
            return term
    return category or ""


def _query_plan(product: str, brand: str, category: str) -> list[tuple[str, str]]:
    """返回 [(检索词, 匹配级别)]。级别：exact > name > generic。"""
    plan: list[tuple[str, str]] = []
    if brand and brand not in product:
        plan.append((f"{brand} {product}", "exact"))
    if brand:
        plan.append((f"{brand} {product.replace(brand, '', 1).strip() or product}", "exact"))
    plan.append((product, "name"))

    generic = _generic_term(product, category)
    if generic and generic != product:
        plan.append((f"{generic} 商品实拍", "generic"))
        plan.append((generic, "generic"))

    # 去重且保持顺序
    seen, ordered = set(), []
    for query, level in plan:
        query = query.strip()
        if query and query not in seen:
            seen.add(query)
            ordered.append((query, level))
    return ordered


def _search_with_fallback(product: str, brand: str, category: str, count: int) -> dict:
    tried = []
    for provider_name in _provider_order():
        if _PROVIDER_DOWN_UNTIL.get(provider_name, 0) > time.time():
            continue
        provider = _PROVIDERS[provider_name]
        for query, level in _query_plan(product, brand, category):
            tried.append(f"{provider_name}:{query}")
            try:
                images = provider(query, product, brand, category, count)
            except Exception as exc:
                logger.warning(f"图片源 {provider_name} 查询“{query}”失败：{exc}")
                _PROVIDER_DOWN_UNTIL[provider_name] = time.time() + FAIL_COOLDOWN
                images = []
                break  # 图源整体不可用，直接换下一个图源
            if images:
                # 以实际命中图片的标题关系为准（而不是检索词级别），避免把同品牌其他型号谎称成实拍
                level = images[0].get("match_level") or level
                return {
                    "status": "success",
                    "product": product,
                    "query": query,
                    "match_level": level,
                    "provider": provider_name,
                    "images": images,
                    "providers_tried": tried,
                }
    return {
        "status": "no_data",
        "product": product,
        "message": "没有检索到可用的商品图片。",
        "images": [],
        "providers_tried": tried,
    }


def search_product_photos_impl(product: str, brand: str = "", category: str = "",
                               count: int = 1) -> dict:
    product = _text(product, 60)
    brand = _text(brand, 20)
    category = _text(category, 20)
    if not product:
        return {"status": "error", "message": "product 不能为空", "images": []}

    count = max(1, min(int(count or 1), 3))
    cache_key = (product, brand, category, count)
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]

    result = _search_with_fallback(product, brand, category, count)
    _CACHE[cache_key] = (time.time() + CACHE_TTL, result)
    return result


# ==================== MCP 服务器 ====================
def create_image_mcp_server():
    image_mcp = FastMCP(
        name="ImageSearchTools",
        instructions=(
            "商品实拍图检索工具。返回图片 URL、原图页、来源站点和授权说明，"
            "供电商种草文案渲染使用；只返回真实检索结果，不生成图片。"
        ),
    )

    @image_mcp.tool(
        name="search_product_photos",
        description=(
            "检索某个商品的真实实拍图（商品图/开箱图）。"
            "参数：product(商品名称，如“声海 X1 真无线蓝牙耳机”), "
            "brand(品牌，可选，如“声海”), category(品类，可选，如“数码配件”), "
            "count(返回数量，1~3，默认1)。"
            "返回 JSON：status、provider、match_level(exact/name/generic)、"
            "images（title/image_url/page_url/site/license 等）。"
            "match_level=generic 表示只找到同类商品参考图，不是该商品本身。"
            "必须原样使用 image_url，不要自己拼接或编造图片地址。"
        ),
    )
    def search_product_photos(product: str, brand: str = "", category: str = "",
                              count: int = 1) -> str:
        logger.info(
            f"商品图片检索: product={product}, brand={brand}, "
            f"category={category}, count={count}"
        )
        result = search_product_photos_impl(product, brand, category, count)
        logger.info(
            f"商品图片检索结果: {result.get('status')} / "
            f"{result.get('provider', '-')} / {result.get('match_level', '-')} / "
            f"{len(result.get('images', []))} 张"
        )
        return json.dumps(result, ensure_ascii=False)

    logger.info("=== 图片 MCP 服务器信息 ===")
    logger.info(f"图源顺序: {', '.join(_provider_order())}")
    print("服务器已启动，请访问 http://127.0.0.1:8004/mcp")
    image_mcp.run(transport="http", host="0.0.0.0", port=8004)


if __name__ == "__main__":
    create_image_mcp_server()
