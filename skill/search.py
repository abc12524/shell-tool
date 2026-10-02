# skill: search
# description: 百度搜索 / 百科查询（百度千帆引擎）
# usage:
#   arguments.mode = raw | summary | baike | baikelist
#     raw        原始搜索结果（默认）
#     summary    网页摘要（AI 总结 + 来源，最快推荐）
#     baike      百科词条详情（摘要 + 信息卡）
#     baikelist  百科搜索列表（按标题）
#   arguments.query = 搜索关键词或词条名
#   示例: {"mode": "summary", "query": "今天北京天气"}
from core.tools.search_tools import baidu_search


def run(arguments):
    a = arguments or {}
    return baidu_search(a.get("mode", "raw"), a.get("query", ""))
