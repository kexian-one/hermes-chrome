"""One-shot check that ecom-best-source SKILL.md loads + contains key markers."""
from agent.skill_loader import SkillRegistry
from pathlib import Path

r = SkillRegistry(Path('skills'))
s = r.load_full('ecom-best-source')
print(f'body chars: {len(s.body)}')
print(f'desc chars: {len(s.description)}')
missing = []
for marker in ['不要依赖外部', '优先接口/MCP', 'scripts/sourcing_rules.py',
               'scripts/ecom_config.py', 'scripts/fetch_candidates.py',
               'scripts/keyword_builder.py',
               'references/api_mcp_sources.md', 'references/final_filter_rules.md',
               '价格 71.43%', '综合服务分 28.57%', '移除发票能力', 'UTF-8 BOM']:
    found = marker in s.body
    print(f'{"✓" if found else "✗"} {marker}')
    if not found:
        missing.append(marker)
if missing:
    raise SystemExit(f'missing markers: {", ".join(missing)}')
