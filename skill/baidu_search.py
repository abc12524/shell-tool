# skill: baidu_search
# description: 百度搜索 / 百科查询（百度千帆引擎，进程内直连）
# status: enable
# usage:
#   arguments.mode = raw | summary | search | baike | baikelist | quota
#     raw        百度搜索原始结果（默认，50 次/天）
#     summary    网页摘要（AI 总结 + 来源，最快推荐，100 次/天）
#     search     智能搜索生成（LLM 总结，较慢，100 次/天）
#     baike      百科词条详情（摘要 + 信息卡，不限）
#     baikelist  百科搜索列表（按标题，100 次/天）
#     quota      查看配额说明
#   arguments.query = 搜索关键词或词条名（quota 模式可省略）
#   示例: {"mode": "summary", "query": "今天北京天气"}
import json
import os

import requests

from core.tools.envelope import ok, error

_HEADERS = {"Content-Type": "application/json"}
_VALID_MODES = ("raw", "summary", "search", "baike", "baikelist", "quota")

_QUOTA = (
    "百度搜索(web_search): 50次/天 | 智能搜索生成(chat/completions): 100次/天 | "
    "网页摘要(web_summary): 100次/天 | 百科词条(get_content): 不限 | "
    "百科搜索(get_list_by_title): 100次/天（具体余量请登录百度千帆控制台查看）"
)


def _key():
    return os.environ.get("BAIDU_QIANFAN_KEY", "")


def _req(path, payload, timeout=60):
    headers = dict(_HEADERS)
    headers["X-Appbuilder-Authorization"] = f"Bearer {_key()}"
    r = requests.post(f"https://qianfan.baidubce.com{path}", headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _raw(query):
    """百度搜索（原始结果，web_search 专用端点）"""
    headers = dict(_HEADERS)
    headers["X-Appbuilder-Authorization"] = f"Bearer {_key()}"
    r = requests.post(
        "https://qianfan.baidubce.com/v2/ai_search/web_search",
        headers=headers,
        json={
            "messages": [{"content": query, "role": "user"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": 10}],
        },
        timeout=30,
    )
    r.raise_for_status()
    refs = r.json().get("references", [])
    results = [
        {"title": x.get("title"), "url": x.get("url"),
         "site": x.get("website", ""), "desc": x.get("snippet", "")}
        for x in refs
    ]
    return {"success": True, "type": "raw_search", "results": results}


def _summary(query):
    """网页摘要（AI 总结 + 来源，流式 SSE）"""
    headers = dict(_HEADERS)
    headers["X-Appbuilder-Authorization"] = f"Bearer {_key()}"
    payload = {
        "messages": [{"role": "user", "content": query}],
        "stream": True,
        "resource_type_filter": [
            {"type": "web", "top_k": int(os.environ.get("WEB_K", "10"))},
            {"type": "video", "top_k": 0},
            {"type": "image", "top_k": 0},
        ],
    }
    answer = ""
    refs = []
    with requests.post(
        "https://qianfan.baidubce.com/v2/ai_search/web_summary",
        headers=headers, json=payload, stream=True, timeout=30
    ) as resp:
        resp.raise_for_status()
        buf = b""
        for chunk in resp.iter_content(chunk_size=4096):
            buf += chunk
            while b"\n\n" in buf:
                line, buf = buf.split(b"\n\n", 1)
                s = line.decode(errors="replace").strip()
                if not s.startswith("data: "):
                    continue
                try:
                    d = json.loads(s[6:])
                except json.JSONDecodeError:
                    continue
                if "references" in d:
                    refs = d["references"]
                for c in d.get("choices", []):
                    if c.get("delta", {}).get("content"):
                        answer += c["delta"]["content"]
    sources = [
        {"title": r.get("title"), "url": r.get("url"),
         "site": r.get("website", ""), "desc": r.get("snippet", "")}
        for r in refs[:10]
    ]
    return {"success": True, "answer": answer.strip(), "type": "summary", "sources": sources}


def _search(query):
    """智能搜索生成（LLM 总结）"""
    flt = [{"type": "web", "top_k": int(os.environ.get("WEB_K", "5"))}]
    d = _req("/v2/ai_search/chat/completions", {
        "messages": [{"content": query, "role": "user"}],
        "search_source": os.environ.get("SEARCH_SOURCE", "baidu_search_v2"),
        "search_recency_filter": os.environ.get("RECENCY", "year"),
        "stream": False,
        "model": os.environ.get("MODEL", "ernie-4.5-turbo-32k"),
        "enable_deep_search": os.environ.get("DEEP", "").lower() in ("true", "1"),
        "temperature": 0.11, "top_p": 0.55,
        "search_mode": "auto", "enable_reasoning": True,
        "enable_corner_markers": True,
        "resource_type_filter": flt,
    }, timeout=120 if os.environ.get("DEEP") else 60)
    out = {"success": True, "answer": d["choices"][0]["message"]["content"]}
    out["usage"] = {k: d["usage"][k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    out["sources"] = [
        {"title": r.get("title"), "url": r.get("url"),
         "site": r.get("website", r.get("site_name", ""))}
        for r in d.get("references", [])[:10]
    ]
    if "followup_queries" in d:
        out["followup"] = d["followup_queries"]
    return out


def _baike(query):
    """百科词条详情（摘要 + 信息卡）"""
    r = requests.get(
        "https://appbuilder.baidu.com/v2/baike/lemma/get_content",
        params={"search_type": "lemmaTitle", "search_key": query},
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json().get("result", {})
    if not d:
        return None
    cards = {c["name"]: c["value"] for c in d.get("card", []) if "name" in c}
    return {
        "success": True, "title": d.get("lemma_title"),
        "desc": d.get("lemma_desc"), "url": d.get("url"),
        "summary": (d.get("summary") or "")[:2000],
        "info": cards, "img": d.get("pic_url"),
    }


def _baikelist(query):
    """百度百科搜索（按标题搜列表）"""
    r = requests.get(
        "https://appbuilder.baidu.com/v2/baike/lemma/get_list_by_title",
        params={"lemma_title": query, "top_k": 5},
        headers={"Authorization": f"Bearer {_key()}"},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("result", [])
    if not results:
        return None
    return {
        "success": True, "total": len(results), "type": "baike_search",
        "results": [
            {"lemma_id": x["lemma_id"], "title": x.get("lemma_title", ""),
             "desc": x.get("lemma_desc", ""), "url": x.get("url", "")}
            for x in results
        ],
    }


def run(arguments):
    a = arguments or {}
    mode = (a.get("mode") or "raw").strip()
    query = (a.get("query") or "").strip()

    # 未知 mode：与旧 CLI 行为一致，整体作为 raw 关键词
    if mode not in _VALID_MODES:
        query = " ".join(p for p in (mode, query) if p).strip()
        mode = "raw"

    if mode == "quota":
        return ok({"quota": _QUOTA})

    if not query:
        return error(f"缺少 query（mode={mode} 需要关键词）", code="missing_argument")

    try:
        if mode == "raw":
            return ok(_raw(query))
        if mode == "summary":
            return ok(_summary(query))
        if mode == "search":
            return ok(_search(query))
        if mode == "baike":
            out = _baike(query)
            return ok(out) if out else error(f"未找到词条「{query}」", code="not_found")
        if mode == "baikelist":
            out = _baikelist(query)
            return ok(out) if out else error(f"未找到词条「{query}」", code="not_found")
    except Exception as e:
        return error(f"百度搜索失败 - {e}", code="search_failed")
    return error(f"未知 mode '{mode}'", code="unknown_action")
