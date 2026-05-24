from __future__ import annotations

from agent.knowledge_store import KnowledgeStore
from agent.llm_client import LLMClient


_MERGE_PROMPT_TEMPLATE = """\
你是 knowledge curator。下面是同一主题 [{topic}] 在多台机器上的观察笔记。

{machine_sections}

当前 curated 版本(如果有):
{curated_section}

任务:
- 合并这些笔记的有效信息,去重 + 解决冲突
- 保留矛盾点(标明 "[来源:pc-X 说 A,pc-Y 说 B,以哪个为准未知]")
- 不要无中生有,只整合现有信息
- 输出新的 curated markdown 内容(从 frontmatter 开始)

frontmatter 字段:
- topic: 主题名
- last_consolidated: ISO 8601 时间
- machine_sources: 列表,例如 [pc-jianghu, pc-beijing]
- version: 整数,自动递增(首次为 1,有旧 curated 则 +1)

正文用 markdown 章节结构。
"""


def _build_prompt(topic: str, machine_views: dict[str, str], curated: str | None) -> str:
    sections: list[str] = []
    for machine_id, content in machine_views.items():
        sections.append(f"[machine: {machine_id}]\n{content}")
    machine_sections = "\n\n".join(sections) if sections else "(无机器笔记)"
    curated_section = curated if curated else "(空)"
    return _MERGE_PROMPT_TEMPLATE.format(
        topic=topic,
        machine_sections=machine_sections,
        curated_section=curated_section,
    )


async def consolidate_topic(topic: str, store: KnowledgeStore, llm: LLMClient) -> bool:
    machine_views = store.list_machine_views(topic)
    if not machine_views:
        return False

    curated = store.load_curated(topic)
    prompt = _build_prompt(topic, machine_views, curated)

    response = await llm.chat(
        messages=[{"role": "user", "content": prompt}],
    )

    merged = response.text
    if not merged or not merged.strip():
        return False

    store.write_curated(topic, merged.strip())
    return True


async def run_daily_consolidation(store: KnowledgeStore, llm: LLMClient) -> dict[str, bool]:
    topics = store.list_topics()
    results: dict[str, bool] = {}
    for topic in topics:
        try:
            updated = await consolidate_topic(topic, store, llm)
            results[topic] = updated
        except Exception as exc:
            print(f"[knowledge_sink] consolidate_topic({topic!r}) failed: {exc}")
            results[topic] = False
    return results
