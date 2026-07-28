#!/usr/bin/env bash
#
# install.sh — Configure your MCP client to use the slacking.biz MCP server.
#
# Usage:
#   bash install.sh                    # Interactive setup
#   bash install.sh --client <type>    # Direct setup for: claude, cursor, windsurf, generic
#
# The slacking.biz MCP server is already running at https://slacking.biz/mcp
# No local installation is needed — just configure your client to point there.
#
# ---------------------------------------------------------------------------

set -euo pipefail

MCP_ENDPOINT="https://slacking.biz/mcp"
DOCS_URL="https://slacking.biz/docs"
DASHBOARD_URL="https://slacking.biz/marketplace"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║        slacking.biz — MCP Client Configuration             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Endpoint:  $MCP_ENDPOINT"
echo "  Docs:      $DOCS_URL"
echo "  Dashboard: $DASHBOARD_URL"
echo ""

# ── Detect / handle client type ──────────────────────────────────────────

CLIENT="${1:-}"

if [ -z "$CLIENT" ] || [ "$CLIENT" = "--client" ] && [ -z "${2:-}" ]; then
    echo "Select your MCP client:"
    echo "  1) Claude Desktop"
    echo "  2) Cursor"
    echo "  3) Windsurf"
    echo "  4) Generic (any MCP-compatible client)"
    echo "  5) Show curl test commands"
    echo ""
    read -rp "Enter number [1-5]: " CHOICE
    case "$CHOICE" in
        1) CLIENT="claude" ;;
        2) CLIENT="cursor" ;;
        3) CLIENT="windsurf" ;;
        4) CLIENT="generic" ;;
        5) CLIENT="curl" ;;
        *) echo "Invalid choice."; exit 1 ;;
    esac
elif [ "$CLIENT" = "--client" ]; then
    CLIENT="$2"
fi

# ── Generate config snippet ──────────────────────────────────────────────

case "$CLIENT" in
    claude)
        echo ""
        echo "📝 Add this to your Claude Desktop config file"
        echo "   (claude_desktop_config.json):"
        echo ""
        cat <<- CONFIG
{
  "mcpServers": {
    "slacking-biz": {
      "type": "streamable-http",
      "url": "${MCP_ENDPOINT}",
      "env": {}
    }
  }
}
CONFIG
        echo ""
        echo "   Location:"
        echo "   macOS: ~/Library/Application Support/Claude/claude_desktop_config.json"
        echo "   Linux: ~/.config/Claude/claude_desktop_config.json"
        echo "   Windows: %APPDATA%\\Claude\\claude_desktop_config.json"
        ;;

    cursor)
        echo ""
        echo "📝 In Cursor, go to Settings → Features → MCP Servers and add:"
        echo ""
        echo "   Name:     slacking-biz"
        echo "   Type:     streamable-http"
        echo "   URL:      ${MCP_ENDPOINT}"
        ;;

    windsurf)
        echo ""
        echo "📝 Add this to your Windsurf MCP config file:"
        echo ""
        cat <<- CONFIG
{
  "mcpServers": {
    "slacking-biz": {
      "type": "streamable-http",
      "url": "${MCP_ENDPOINT}"
    }
  }
}
CONFIG
        ;;

    generic)
        echo ""
        echo "📝 Configure your MCP client with:"
        echo ""
        echo "   Type: streamable-http"
        echo "   URL:  ${MCP_ENDPOINT}"
        echo ""
        echo "   If your client uses JSON config format:"
        cat <<- CONFIG
{
  "mcpServers": {
    "slacking-biz": {
      "type": "streamable-http",
      "url": "${MCP_ENDPOINT}"
    }
  }
}
CONFIG
        ;;

    curl)
        echo ""
        echo "🧪 Test the MCP endpoint with curl:"
        echo ""
        echo "  # 1. Initialize handshake"
        echo "  curl -X POST ${MCP_ENDPOINT} \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"client\",\"version\":\"1.0\"}}}'"
        echo ""
        echo "  # 2. List tools"
        echo "  curl -X POST ${MCP_ENDPOINT} \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/list\",\"params\":{}}'"
        echo ""
        echo "  # 3. Get AAPL health"
        echo "  curl -X POST ${MCP_ENDPOINT} \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":\"3\",\"method\":\"tools/call\",\"params\":{\"name\":\"get_company_health\",\"arguments\":{\"ticker\":\"AAPL\"}}}'"
        ;;

    *)
        echo "Unknown client: $CLIENT"
        echo "Supported: claude, cursor, windsurf, generic, curl"
        exit 1
        ;;
esac

echo ""
echo "✅ Done! For more details visit:"
echo "   📚 $DOCS_URL"
echo "   🏪 $DASHBOARD_URL"
echo ""
