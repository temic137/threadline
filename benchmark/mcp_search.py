"""Print what an agent receives from Threadline's search_context tool, exactly as sent over MCP."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handoff_sessions import Journal, mcp_response  # noqa: E402

project, query = sys.argv[1], sys.argv[2]
journal = Journal(Path.home() / ".handoff" / "handoff.db")
request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "search_context", "arguments": {"project": project, "query": query}}}
print(mcp_response(request, journal)["result"]["content"][0]["text"])
