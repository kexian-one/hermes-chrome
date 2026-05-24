"""Dry-run the chase skill flow against ONE merchant: navigate → search →
click 旺旺 → land on IM tab. Stops BEFORE sending any messages.

Use this to verify the UI flow works without LLM variability. If this script
succeeds, the SKILL.md steps are correct; if it fails, you'll see exactly
which step broke.

Usage:
    python -m scripts.chase_dry_run --order 3293626225672531283 --shop "重庆致华电子商务有限公司"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agent.mcp_client import OpenClaudeInChromeClient


# Reusable JS snippets (same shapes as SKILL.md)
_WALK_FN = """\
function* walk(node) {
  if (!node) return;
  yield node;
  if (node.shadowRoot) yield* walk(node.shadowRoot);
  for (const c of node.childNodes || []) if (c.nodeType === 1) yield* walk(c);
}
"""


def js_set_order_and_find_search(order_id: str) -> str:
    return _WALK_FN + f"""
(function() {{
  let qInput = null;
  for (const n of walk(document)) {{
    if (n.tagName === 'Q-INPUT' && n.shadowRoot) {{
      const inner = n.shadowRoot.querySelector('input');
      if (inner && /商品名称|订单号/.test(inner.placeholder||'')) {{ qInput = n; break; }}
    }}
  }}
  if (!qInput) return JSON.stringify({{ error: 'Q-INPUT not found' }});

  const inner = qInput.shadowRoot.querySelector('input');
  qInput.value = '{order_id}';
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  inner.focus();
  setter.call(inner, '{order_id}');
  inner.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
  inner.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));

  let btnCenter = null;
  for (const n of walk(document)) {{
    if (n.tagName === 'Q-BUTTON' && /^搜索$/.test((n.textContent||'').trim())) {{
      const r = n.getBoundingClientRect();
      if (r.width > 0) {{
        btnCenter = {{ cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2) }};
        break;
      }}
    }}
  }}
  return JSON.stringify({{ qInputValue: inner.value, btnCenter }});
}})()
"""


def js_find_wangwang(expected_shop: str) -> str:
    # Use unique markers to avoid string formatting collisions with regex chars
    return _WALK_FN + f"""
(function() {{
  const sellers = [];
  for (const n of walk(document)) {{
    if (n.children && n.children.length === 0
        && (n.textContent||'').includes({json.dumps(expected_shop)})) {{
      sellers.push((n.textContent||'').trim());
    }}
  }}
  const hits = [];
  for (const n of walk(document)) {{
    if (n.tagName === 'WANG-WANG') {{
      const r = n.getBoundingClientRect();
      if (r.width > 0) hits.push({{ cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2) }});
    }}
  }}
  return JSON.stringify({{ sellersFound: sellers.length, iconCenter: hits[0] || null, allIconCount: hits.length }});
}})()
"""


def _extract_text(result) -> str:
    """Pull the FIRST text block's text out of a ToolResult — most tools
    return a single block of JSON. Joining multiple with '\\n' would break
    JSON parsing if a later block is non-JSON."""
    if not result or not result.content:
        return ""
    for block in result.content:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "")
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser(description="Chase skill dry-run (no message sending)")
    parser.add_argument("--order", required=True, help="19-digit order id")
    parser.add_argument("--shop", required=True, help="expected shop name")
    parser.add_argument("--port", type=int, default=18766, help="b2 MCP port (default 18766)")
    parser.add_argument("--worker", default="b2", help="worker id for resolving mcp-server.js path")
    args = parser.parse_args()

    mcp_js = Path(f"./deploy/oicc-{args.worker}/host/mcp-server.js").resolve()
    if not mcp_js.is_file():
        print(f"FAIL: mcp-server.js not found at {mcp_js}")
        return 1

    async with OpenClaudeInChromeClient(port=args.port, mcp_server_js_path=mcp_js) as c:
        print(f"=== STEP 1: get / create tab ===")
        r = await c.call_tool("tabs_context_mcp", {"createIfEmpty": True})
        ctx_text = _extract_text(r)
        print(ctx_text[:400])
        try:
            ctx, _end = json.JSONDecoder().raw_decode(ctx_text.lstrip())
        except json.JSONDecodeError as exc:
            print(f"FAIL: tabs_context_mcp returned non-JSON ({exc})")
            print(f"  first 200 chars: {ctx_text[:200]!r}")
            return 1
        # Get a usable tabId (just take the first one; the extension always returns >= 1)
        tabs = ctx.get("availableTabs") or ctx.get("tabs") or []
        if not tabs:
            print(f"FAIL: no tabs in context")
            return 1
        tab_id = tabs[0].get("id") or tabs[0].get("tabId")
        print(f"using tabId={tab_id}")

        print(f"\n=== STEP 2: navigate to order list ===")
        await c.call_tool("navigate", {"tabId": tab_id, "url": "https://trade.1688.com/order/buyer_order_list.htm"})
        print("navigated, waiting 6s for SPA render...")
        await asyncio.sleep(6)

        print(f"\n=== STEP 3: JS — set qInput + find search button center ===")
        r = await c.call_tool("javascript_tool", {
            "action": "javascript_exec",
            "tabId": tab_id,
            "text": js_set_order_and_find_search(args.order),
        })
        step3_text = _extract_text(r)
        print(f"raw: {step3_text[:500]}")
        try:
            step3 = json.loads(step3_text.strip().strip('"').replace('\\"', '"'))
            if isinstance(step3, str):
                step3 = json.loads(step3)
        except Exception as exc:
            print(f"FAIL: cannot parse Step 3 result: {exc}")
            return 1
        if step3.get("error"):
            print(f"FAIL: Step 3 error: {step3['error']}")
            return 1
        btn = step3.get("btnCenter")
        print(f"  qInputValue={step3.get('qInputValue')!r}")
        print(f"  btnCenter={btn}")
        if not btn:
            print(f"FAIL: search button not found via JS walk")
            return 1

        print(f"\n=== STEP 4: computer.left_click search button at {btn} ===")
        await c.call_tool("computer", {
            "action": "left_click",
            "tabId": tab_id,
            "coordinate": [btn["cx"], btn["cy"]],
        })
        print("clicked, waiting 3s for results...")
        await asyncio.sleep(3)

        print(f"\n=== STEP 5: JS — verify shop + find 旺旺 icon ===")
        r = await c.call_tool("javascript_tool", {
            "action": "javascript_exec",
            "tabId": tab_id,
            "text": js_find_wangwang(args.shop),
        })
        step5_text = _extract_text(r)
        print(f"raw: {step5_text[:500]}")
        try:
            step5 = json.loads(step5_text.strip().strip('"').replace('\\"', '"'))
            if isinstance(step5, str):
                step5 = json.loads(step5)
        except Exception as exc:
            print(f"FAIL: cannot parse Step 5 result: {exc}")
            return 1
        sellers_found = step5.get("sellersFound", 0)
        icon = step5.get("iconCenter")
        all_icons = step5.get("allIconCount", 0)
        print(f"  sellersFound={sellers_found} (looking for {args.shop!r})")
        print(f"  iconCenter={icon}")
        print(f"  allIconCount={all_icons}")
        if not icon:
            print(f"FAIL: 旺旺 icon not found via JS walk")
            return 1
        if sellers_found == 0:
            print(f"WARN: shop name not matched in DOM — search may have returned 0 results. Continuing anyway.")

        print(f"\n=== STEP 6: computer.left_click 旺旺 icon at {icon} ===")
        await c.call_tool("computer", {
            "action": "left_click",
            "tabId": tab_id,
            "coordinate": [icon["cx"], icon["cy"]],
        })
        print("clicked, waiting 4s for new IM tab to open...")
        await asyncio.sleep(4)

        print(f"\n=== STEP 7: find new IM tab ===")
        r = await c.call_tool("tabs_context_mcp", {})
        ctx2_text = _extract_text(r)
        try:
            ctx2, _end = json.JSONDecoder().raw_decode(ctx2_text.lstrip())
        except json.JSONDecodeError as exc:
            print(f"FAIL: tabs_context_mcp #2 returned non-JSON ({exc})")
            print(f"  first 200 chars: {ctx2_text[:200]!r}")
            return 1
        tabs2 = ctx2.get("availableTabs") or ctx2.get("tabs") or []
        im_tab = None
        for t in tabs2:
            url = t.get("url", "")
            if "def_cbu_web_im" in url or "touid=" in url:
                im_tab = t
                break
        if not im_tab:
            print(f"FAIL: no IM tab found. Current tabs:")
            for t in tabs2:
                print(f"  - id={t.get('id') or t.get('tabId')} url={t.get('url', '')[:80]}")
            return 1
        im_tab_id = im_tab.get("id") or im_tab.get("tabId")
        print(f"  IM tab id={im_tab_id}")
        print(f"  url={im_tab.get('url', '')[:200]}")

        print(f"\n=== SUCCESS — reached IM page. Stopped before sending. ===")
        print(f"IM tab id: {im_tab_id} (still open, you can manually verify it has the editor)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
