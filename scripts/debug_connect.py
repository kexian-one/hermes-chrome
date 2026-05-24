import os
import socket
import asyncio

port = int(os.environ.get("MCP_TEST_PORT", "18765"))
print(f"MCP_TEST_PORT env value: {port}")

s = socket.socket()
s.settimeout(0.8)
try:
    s.connect(("127.0.0.1", port))
    print(f"  TCP connect to 127.0.0.1:{port} OK")
    s.close()
except Exception as e:
    print(f"  TCP connect to 127.0.0.1:{port} FAILED: {type(e).__name__}: {e}")

print()
print("Now trying full MCP handshake via agent.mcp_client...")

from agent.mcp_client import OpenClaudeInChromeClient

async def main():
    try:
        async with OpenClaudeInChromeClient(port=port) as client:
            tools = await client.list_tools()
            print(f"  list_tools OK: {len(tools)} tools")
            for t in tools[:8]:
                print(f"    - {t.name}")
    except Exception as e:
        print(f"  FAILED at MCP layer: {type(e).__name__}: {e}")

asyncio.run(main())
