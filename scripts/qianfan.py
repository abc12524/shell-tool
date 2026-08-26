#!/usr/bin/env python3
"""百度千帆 AI 工具集

用法:
  qianfan <关键词>         — 百度搜索(原始结果, 50次/天, 默认模式)
  qianfan summary <关键词>  — 网页摘要(含AI总结+搜索结果, 最快推荐)
  qianfan search <关键词>  — 智能搜索生成(LLM总结, 100次/天, 较慢)
  qianfan raw <关键词>     — 百度搜索(原始结果, 50次/天)
  qianfan baike <词条名>  — 百科词条详情(含摘要+信息卡)
  qianfan baikelist <词>  — 百度百科搜索(按标题搜列表, 100次/天)
  qianfan quota           — 查看剩余配额说明
"""
import json, os, sys, requests
from dotenv import load_dotenv
load_dotenv()
key = os.environ.get('BAIDU_QIANFAN_KEY', '')
HEADERS = {'Content-Type': 'application/json'}

mode = sys.argv[1] if len(sys.argv) > 1 else ""
query = ' '.join(sys.argv[2:]) if len(sys.argv) > 2 else ''

# 默认无子命令时走 raw 搜索
if mode and mode not in ("search", "summary", "raw", "baike", "baikelist", "quota"):
    query = ' '.join(sys.argv[1:])
    mode = "raw"


def req(path, payload, timeout=60):
    h = dict(HEADERS)
    h['X-Appbuilder-Authorization'] = f'Bearer {key}'
    r = requests.post(f'https://qianfan.baidubce.com{path}', headers=h, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ===== 配额说明 =====
if mode == "quota":
    print("百度搜索(web_search): 50次/天 | 智能搜索生成(chat/completions): 100次/天 | 网页摘要(web_summary): 100次/天 | 百科词条(get_content): 不限 | 百科搜索(get_list_by_title): 100次/天")
    print("(具体余量请登录百度千帆控制台查看)")
    sys.exit(0)


# ===== 百科词条详情（完整内容）=====
if mode == "baike":
    if not query:
        print('{"error": "Usage: qianfan baike <词条名>"}')
        sys.exit(1)
    try:
        r = requests.get("https://appbuilder.baidu.com/v2/baike/lemma/get_content",
            params={"search_type": "lemmaTitle", "search_key": query},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=15)
        r.raise_for_status()
        d = r.json().get("result", {})
        if not d:
            print(json.dumps({"error": f"未找到词条「{query}」"}, ensure_ascii=False))
            sys.exit(1)
        cards = {}
        for c in d.get("card", []):
            if "name" in c:
                cards[c["name"]] = c["value"]
        out = {"success": True, "title": d.get("lemma_title"),
               "desc": d.get("lemma_desc"), "url": d.get("url"),
               "summary": (d.get("summary") or "")[:2000],
               "info": cards, "img": d.get("pic_url")}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


# ===== 百度百科搜索（按标题搜列表）=====
if mode == "baikelist":
    if not query:
        print('{"error": "Usage: qianfan baikelist <关键词>"}')
        sys.exit(1)
    try:
        r = requests.get("https://appbuilder.baidu.com/v2/baike/lemma/get_list_by_title",
            params={"lemma_title": query, "top_k": 5},
            headers={"Authorization": f"Bearer {key}"},
            timeout=15)
        r.raise_for_status()
        d = r.json()
        results = d.get("result", [])
        if not results:
            print(json.dumps({"error": f"未找到词条「{query}」"}, ensure_ascii=False))
            sys.exit(1)
        out = {"success": True, "total": len(results), "type": "baike_search"}
        out["results"] = []
        for r2 in results:
            out["results"].append({
                "lemma_id": r2["lemma_id"],
                "title": r2.get("lemma_title", ""),
                "desc": r2.get("lemma_desc", ""),
                "url": r2.get("url", "")
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


# ===== Web摘要（AI总结+搜索结果，流式SSE）=====
if mode == "summary":
    if not query:
        print('{"error": "Usage: qianfan summary <关键词>"}')
        sys.exit(1)
    try:
        h = dict(HEADERS)
        h["X-Appbuilder-Authorization"] = f"Bearer {key}"
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
            headers=h, json=payload, stream=True, timeout=30
        ) as resp:
            resp.raise_for_status()
            buf = b""
            for chunk in resp.iter_content(chunk_size=4096):
                buf += chunk
                while b"\n\n" in buf:
                    line, buf = buf.split(b"\n\n", 1)
                    s = line.decode(errors="replace").strip()
                    if s.startswith("data: "):
                        try:
                            d2 = json.loads(s[6:])
                            if "references" in d2:
                                refs = d2["references"]
                            for c in d2.get("choices", []):
                                if c.get("delta", {}).get("content"):
                                    answer += c["delta"]["content"]
                        except json.JSONDecodeError:
                            pass
        out = {"success": True, "answer": answer.strip(), "type": "summary"}
        out["sources"] = []
        for r in refs[:10]:
            out["sources"].append({
                "title": r.get("title"), "url": r.get("url"),
                "site": r.get("website", ""), "desc": r.get("snippet", "")
            })
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


# ===== 智能搜索生成 =====
if mode == "search":
    if not query:
        print('{"error": "Usage: qianfan search <关键词>"}')
        sys.exit(1)
    src = os.environ.get("SEARCH_SOURCE", "baidu_search_v2")
    flt = [{"type": "web", "top_k": int(os.environ.get("WEB_K", "5"))}]
    try:
        d = req("/v2/ai_search/chat/completions", {
            "messages": [{"content": query, "role": "user"}],
            "search_source": src,
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
        out["sources"] = []
        for r in d.get("references", [])[:10]:
            out["sources"].append({"title": r.get("title"), "url": r.get("url"), "site": r.get("website", r.get("site_name", ""))})
        if "followup_queries" in d:
            out["followup"] = d["followup_queries"]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


# ===== 百度搜索（原始结果，专用web_search端点）=====
if mode == "raw":
    if not query:
        print('{"error": "Usage: qianfan raw <关键词>"}')
        sys.exit(1)
    try:
        h = dict(HEADERS)
        h["X-Appbuilder-Authorization"] = f"Bearer {key}"
        r = requests.post("https://qianfan.baidubce.com/v2/ai_search/web_search",
            headers=h, json={
                "messages": [{"content": query, "role": "user"}],
                "search_source": "baidu_search_v2",
                "resource_type_filter": [{"type": "web", "top_k": 10}],
            }, timeout=30)
        r.raise_for_status()
        d = r.json()
        out = {"success": True, "type": "raw_search"}
        out["results"] = []
        for r2 in d.get("references", []):
            out["results"].append({"title": r2.get("title"), "url": r2.get("url"),
                                   "site": r2.get("website", ""), "desc": r2.get("snippet", "")})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
