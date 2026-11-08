"""Real online tools used by the MCP server.

- Search / Fetch: DeepSeek's own `/v1/tools/search` and `/v1/tools/fetch`.
- Weather: Open-Meteo (no key).
No mock provider, no third-party search keys.
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

from .. import config
from ..net import HttpError, request_json
from ..util import redact_text, truncate


class OnlineError(Exception):
    pass


def _base() -> str:
    base = (os.environ.get("DEEPSEEK_BASE_URL") or config.DEFAULT_BASE_URL).rstrip("/")
    return base


def _key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise OnlineError("未配置 DeepSeek API Key，无法使用联网搜索/抓取")
    return key


def _tools_url(name: str) -> str:
    base = _base()
    if base.endswith("/v1"):
        return f"{base}/tools/{name}"
    return f"{base}/v1/tools/{name}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"MiniAgent/{config.VERSION}",
    }


def _call_json(url: str, method: str, payload: dict | None, timeout: float) -> dict:
    try:
        data = request_json(url, method=method, headers=_headers(), payload=payload, timeout=timeout)
    except HttpError as exc:
        message = redact_text(exc.friendly_message(), [_key()])
        raise OnlineError(f"联网服务请求失败: {message}") from None
    if not isinstance(data, dict):
        raise OnlineError("联网服务返回了非对象结果")
    return data


def search(query: str, count: int = 5) -> dict:
    query = (query or "").strip()
    if not query:
        raise OnlineError("搜索词不能为空")
    count = max(1, min(int(count or 5), 10))
    data = _call_json(
        _tools_url("search"),
        "POST",
        {
            "query": query[:128],
            "count": count,
            "need_content": False,
        },
        timeout=config.HTTP_TIMEOUT,
    )
    results = data.get("results") or []
    if not isinstance(results, list):
        results = []
    compact = []
    sources = []
    for item in results[:count]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or url or "结果")
        compact.append(
            {
                "title": title,
                "url": url,
                "snippet": truncate(str(item.get("snippet") or ""), 300),
                "summary": truncate(str(item.get("summary") or ""), 600),
                "date": item.get("date"),
                "site_name": item.get("site_name"),
            }
        )
        if url:
            sources.append({"title": title[:200], "url": url})
    summary = (
        f"联网搜索「{query}」返回 {len(compact)} 条结果"
        if compact
        else f"联网搜索「{query}」没有返回结果"
    )
    return {
        "summary": summary,
        "data": {"query": query, "results": compact, "provider": "deepseek-search"},
        "sources": sources,
    }


def fetch(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise OnlineError("url 必须是 http/https 地址")
    data = _call_json(
        _tools_url("fetch"),
        "POST",
        {"url": url},
        timeout=config.HTTP_TIMEOUT,
    )
    title = str(data.get("title") or url)
    content = truncate(str(data.get("content") or ""), 8000)
    return {
        "summary": f"已抓取网页「{title}」{len(content)} 字符",
        "data": {"title": title, "url": url, "content": content},
        "sources": [{"title": title[:200], "url": url}],
    }


_WMO = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def _wmo(code) -> str:
    try:
        return _WMO.get(int(code), "未知天气")
    except (TypeError, ValueError):
        return "未知天气"


def weather(city: str, days: int = 1) -> dict:
    city = (city or "").strip()
    if not city:
        raise OnlineError("城市不能为空")
    try:
        days = max(1, min(int(days or 1), 3))
    except (TypeError, ValueError):
        days = 1
    geocode_url = os.environ.get(
        "OPEN_METEO_GEOCODE_URL",
        "https://geocoding-api.open-meteo.com/v1/search",
    )
    forecast_url = os.environ.get(
        "OPEN_METEO_FORECAST_URL",
        "https://api.open-meteo.com/v1/forecast",
    )
    try:
        geo = request_json(
            geocode_url + "?" + urlencode(
                {"name": city, "count": 1, "language": "zh", "format": "json"}
            ),
            method="GET",
            headers={"Accept": "application/json", "User-Agent": f"MiniAgent/{config.VERSION}"},
            timeout=config.WEATHER_TIMEOUT,
        )
    except HttpError as exc:
        raise OnlineError(f"Open-Meteo 城市查询失败: {exc.friendly_message()}") from None
    results = geo.get("results") if isinstance(geo, dict) else None
    if not results:
        raise OnlineError(f"找不到城市：{city}")
    place = results[0]
    latitude = place.get("latitude")
    longitude = place.get("longitude")
    if latitude is None or longitude is None:
        raise OnlineError(f"城市坐标缺失：{city}")
    try:
        forecast = request_json(
            forecast_url + "?" + urlencode(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": days,
                }
            ),
            method="GET",
            headers={"Accept": "application/json", "User-Agent": f"MiniAgent/{config.VERSION}"},
            timeout=config.WEATHER_TIMEOUT,
        )
    except HttpError as exc:
        raise OnlineError(f"Open-Meteo 天气查询失败: {exc.friendly_message()}") from None

    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    location = place.get("name") or city
    admin = place.get("admin1") or place.get("country") or ""
    if admin:
        location = f"{location}，{admin}"
    current_text = (
        f"{location} 当前 {current.get('temperature_2m', '?')}°C，"
        f"{_wmo(current.get('weather_code'))}，"
        f"湿度 {current.get('relative_humidity_2m', '?')}%，"
        f"风速 {current.get('wind_speed_10m', '?')} km/h"
    )
    daily_rows = []
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    rains = daily.get("precipitation_probability_max") or []
    for index, date in enumerate(dates[:days]):
        row = {
            "date": date,
            "weather": _wmo(codes[index] if index < len(codes) else None),
            "high": highs[index] if index < len(highs) else None,
            "low": lows[index] if index < len(lows) else None,
            "precipitation_probability": rains[index] if index < len(rains) else None,
        }
        daily_rows.append(row)
    if daily_rows:
        first = daily_rows[0]
        current_text += (
            f"；今日 {first.get('low')}–{first.get('high')}°C，"
            f"降水概率 {first.get('precipitation_probability')}%"
        )
    return {
        "summary": current_text,
        "data": {
            "location": location,
            "latitude": latitude,
            "longitude": longitude,
            "current": current,
            "daily": daily_rows,
            "provider": "open-meteo",
        },
        "sources": [
            {"title": "Open-Meteo", "url": "https://open-meteo.com/"},
        ],
    }


TOOL_SCHEMAS = {
    "get_weather": {
        "name": "get_weather",
        "description": "查询城市实时天气和未来 1–3 天预报（Open-Meteo 真实数据）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，例如 北京"},
                "days": {
                    "type": "integer",
                    "description": "预报天数 1–3，默认 1",
                    "minimum": 1,
                    "maximum": 3,
                },
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
    "web_search": {
        "name": "web_search",
        "description": "联网搜索新闻、实时事实和外部资料，返回标题、URL、摘要（DeepSeek Search）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "count": {
                    "type": "integer",
                    "description": "返回条数 1–10，默认 5",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "抓取网页正文，返回 Markdown 文本（DeepSeek Fetch）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 网页地址"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def dispatch(name: str, arguments: dict) -> dict:
    if name == "get_weather":
        return weather(str(arguments.get("city") or ""), int(arguments.get("days") or 1))
    if name == "web_search":
        return search(str(arguments.get("query") or ""), int(arguments.get("count") or 5))
    if name == "fetch_url":
        return fetch(str(arguments.get("url") or ""))
    raise OnlineError(f"未知联网工具：{name}")
