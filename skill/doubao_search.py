# skill: doubao_search
# description: Feedcoop 豆包搜索 API（web_search / global_search），返回结构化网页结果
# usage:
#   arguments.query = 搜索关键词（必填，1~100 字符，不支持多词）
#   arguments.count = 返回条数，默认 5；web 上限 50、image 上限 5，global 服务端恒返 10 条、由本地按 count 截断
#   arguments.api = web（默认，web_search）| global（global_search）
#   arguments.search_type = 搜索类型，默认 web（web | image）
#   arguments.need_content = true|false，服务端过滤：只返回有正文的结果，默认 false
#   arguments.include_content = true|false，结果里是否带正文字段 content，默认 false（正文可达上万字符，慎开）
#   arguments.need_url = true|false，服务端过滤：只返回带 URL 的结果，默认 true
#   arguments.content_format = 正文格式 text（默认）| markdown（include_content=true 时可见）
#   arguments.site = 限定站点（如 example.com），多个用 | 分隔、最多 20 个，默认不限
#   arguments.block = 屏蔽站点，多个用 | 分隔、最多 5 个，默认空
#   arguments.auth_info = 1 只返回非常权威的结果，0 不限（默认 0）
#   arguments.time_range = OneDay|OneWeek|OneMonth|OneYear 或 YYYY-MM-DD..YYYY-MM-DD，默认空
#   arguments.industry = 行业限定 finance|game|gov，默认空
#   arguments.q_rewrite = true|false，是否改写 query（更慢），默认 false
#   arguments.timeout = 请求超时秒数，默认 20
#   示例: {"query": "武汉今天天气", "count": 3}
#   注意: 该 API 静默忽略未知字段——参数名写错不报错、只是不生效。过滤器必须用 Filter.Sites /
#         Filter.BlockHosts / Filter.AuthInfoLevel，正文格式用 ContentFormats，改写用 QueryControl.QueryRewrite
import json
import os

import requests

from core.tools.envelope import error, ok

URL_WEB = "https://open.feedcoopapi.com/search_api/web_search"
URL_GLOBAL = "https://open.feedcoopapi.com/search_api/global_search"
ENDPOINTS = {"web": URL_WEB, "global": URL_GLOBAL}
KEY_ENV = "FEEDCOOP_API_KEY"
TIMEOUT = 20
# 服务端条数上限（文档值）：web 50 / image 5
MAX_COUNT = {"web": 50, "image": 5}
_SEARCH_TYPES = ("web", "image")
_TRUTHY = ("1", "true", "yes", "y", "on")


class _MissingKey(ValueError):
    """缺少 FEEDCOOP_API_KEY：单独一类，便于 run() 给出明确的 code。"""


def _as_bool(value, default=False):
    """宽松解析布尔参数：兼容 JSON 布尔与 "true"/"1" 等字符串。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _as_int(value, default):
    """宽松解析整型参数，非法值回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raise_result_error(result: dict):
    """检查 Result 内层错误码：global 端点用 ErrorCode/ErrorMsg 报错，外层 Code 为空。"""
    code = result.get("ErrorCode", result.get("error_code"))
    if code not in (None, 0, "0", ""):
        message = result.get("ErrorMsg") or result.get("error_msg") or ""
        raise RuntimeError(f"API 返回错误码 {code}: {message}")


def search(
    query: str,
    count: int = 5,
    api: str = "web",
    search_type: str = "web",
    need_content: bool = False,
    include_content: bool = False,
    need_url: bool = True,
    need_summary: bool = True,
    site: str = "",
    block: str = "",
    auth_info: int = 0,
    time_range: str = "",
    content_format: str = "text",
    industry: str = "",
    q_rewrite: bool = False,
    timeout: int = TIMEOUT,
) -> list[dict]:
    """调用 Feedcoop 搜索 API，返回规范化结果列表。

    失败时抛异常：_MissingKey 缺少凭证 / ValueError 参数非法 /
    requests.RequestException 网络层 / RuntimeError 业务层，由 run() 统一转成 error 信封。
    need_summary 仅为兼容保留：服务端不识别该字段（Summary 恒返回），故不进请求体。
    """
    url = ENDPOINTS.get(api)
    if url is None:
        raise ValueError(f"未知 api '{api}'，可用: {', '.join(ENDPOINTS)}")

    stype = (search_type or "web").strip().lower()
    if stype not in _SEARCH_TYPES:
        raise ValueError(f"未知 search_type '{search_type}'，可用: {', '.join(_SEARCH_TYPES)}")

    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise _MissingKey(f"未配置环境变量 {KEY_ENV}（在 shell-tool 的 .env 里填写）")

    count = max(1, min(_as_int(count, 5), MAX_COUNT[stype]))

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    body = {
        "Query": query,
        "SearchType": stype,
        "Count": count,
        "Filter": {"NeedContent": need_content, "NeedUrl": need_url},
        "ContentFormats": content_format,
    }
    if site:
        body["Filter"]["Sites"] = site
    if block:
        body["Filter"]["BlockHosts"] = block
    if auth_info:
        body["Filter"]["AuthInfoLevel"] = auth_info
    if time_range:
        body["TimeRange"] = time_range
    if industry:
        body["Industry"] = industry
    if q_rewrite:
        body["QueryControl"] = {"QueryRewrite": True}

    resp = requests.post(url, headers=headers, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    code = data.get("Code", data.get("code", 0))
    if code not in (0, "0", None, ""):
        message = data.get("Message") or data.get("message") or ""
        raise RuntimeError(f"API 返回错误码 {code}: {message}")
    result = data.get("Result")
    if not isinstance(result, dict):
        raise RuntimeError(
            "API 响应缺少 Result: " + json.dumps(data, ensure_ascii=False)[:200]
        )
    _raise_result_error(result)

    if "Documents" in result:
        return _parse_global(result, count)
    if stype == "image" or ("ImageResults" in result and not result.get("WebResults")):
        return _parse_image(result)
    return _parse_web(result, include_content)


def _parse_web(result: dict, include_content: bool = False) -> list[dict]:
    items = []
    for r in result.get("WebResults", []):
        item = {
            "title": r.get("Title", ""),
            "url": r.get("Url", ""),
            "snippet": r.get("Snippet", ""),
            "summary": r.get("Summary", ""),
            "site_name": r.get("SiteName", ""),
            "publish_time": r.get("PublishTime", ""),
            "auth_info": r.get("AuthInfoDes", ""),
            "rank_score": r.get("RankScore", 0),
        }
        if include_content:
            item["content"] = r.get("Content", "")
        items.append(item)
    return items


def _parse_image(result: dict) -> list[dict]:
    items = []
    for r in result.get("ImageResults", []):
        img = r.get("Image") or {}
        items.append({
            "title": r.get("Title", ""),
            "url": r.get("Url", ""),
            "site_name": r.get("SiteName", ""),
            "publish_time": r.get("PublishTime", ""),
            "image_url": img.get("Url", ""),
            "width": img.get("Width", 0),
            "height": img.get("Height", 0),
            "shape": img.get("Shape", ""),
            "blur": img.get("BlurDes", ""),
            "watermark": img.get("Watermark", 0),
        })
    return items


def _parse_global(result: dict, count: int = 0) -> list[dict]:
    docs = []
    for r in result.get("Documents", []):
        snippet = r.get("Snippet")
        if isinstance(snippet, list):
            text = " ".join(
                s.get("Text", "")
                for s in snippet
                if isinstance(s, dict) and s.get("Type") == "text"
            )
        else:
            text = str(snippet or "")
        docs.append({
            "title": r.get("Title", ""),
            "url": r.get("Url", ""),
            "snippet": text,
        })
    # global 端点忽略请求里的 Count（恒返回 10 条），按调用方要求的 count 本地截断
    if count > 0:
        docs = docs[:count]
    return docs


def run(arguments):
    """skill 入口：arguments(dict) -> ok()/error() 信封字符串。"""
    a = arguments or {}
    query = (a.get("query") or "").strip()
    if not query:
        return error("缺少必填参数 query", code="missing_argument")

    api = (a.get("api") or "web").strip().lower()
    if api not in ENDPOINTS:
        return error(
            f"未知 api '{api}'，可用: {', '.join(ENDPOINTS)}", code="bad_request"
        )

    try:
        results = search(
            query,
            count=_as_int(a.get("count", 5), 5),
            api=api,
            search_type=a.get("search_type", "web"),
            need_content=_as_bool(a.get("need_content"), False),
            include_content=_as_bool(a.get("include_content"), False),
            need_url=_as_bool(a.get("need_url"), True),
            need_summary=_as_bool(a.get("need_summary"), True),
            site=a.get("site", ""),
            block=a.get("block", ""),
            auth_info=_as_int(a.get("auth_info", 0), 0),
            time_range=a.get("time_range", ""),
            content_format=a.get("content_format", "text"),
            industry=a.get("industry", ""),
            q_rewrite=_as_bool(a.get("q_rewrite"), False),
            timeout=_as_int(a.get("timeout", TIMEOUT), TIMEOUT),
        )
    except _MissingKey as exc:
        return error(str(exc), code="missing_credential")
    except requests.exceptions.JSONDecodeError as exc:
        return error(f"响应不是合法 JSON: {exc}", code="bad_response")
    except ValueError as exc:
        return error(str(exc), code="bad_request")
    except requests.RequestException as exc:
        return error(f"请求失败: {exc}", code="http")
    except RuntimeError as exc:
        return error(str(exc), code="api_error")

    return ok({"query": query, "api": api, "count": len(results), "results": results})


if __name__ == "__main__":
    import sys

    _query = sys.argv[1] if len(sys.argv) > 1 else "武汉今天天气"
    print(run({"query": _query, "count": 3}))
