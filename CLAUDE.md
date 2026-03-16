# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Basecamp MCP Integration - a FastMCP-powered server that provides MCP (Model Context Protocol) integration for Basecamp 3, enabling Claude Code and Cursor IDE to interact with Basecamp directly.

**Technology Stack:**
- Python 3.8+ with FastMCP (Anthropic's official MCP framework)
- OAuth 2.0 authentication
- RESTful API client for Basecamp 3

## Common Development Commands

### Setup and Authentication
```bash
# Initial setup (creates venv, installs dependencies, creates .env template)
python setup.py

# Authenticate with Basecamp (run OAuth flow)
python oauth_app.py
# Visit http://localhost:8000 to complete OAuth

# Generate Cursor config
python generate_cursor_config.py

# Generate Claude Desktop config
python generate_claude_desktop_config.py
```

### Testing
```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_card_tables.py -v

# Test MCP server manually (FastMCP uses stdio transport)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python basecamp_fastmcp.py
```

### Running the Server
```bash
# Run FastMCP server (stdio transport for MCP clients)
python basecamp_fastmcp.py

# Run legacy CLI server (for debugging)
python mcp_server_cli.py
```

## Architecture

### Core Components

1. **basecamp_fastmcp.py** - Main MCP server using official FastMCP framework
   - 46 MCP tools (@mcp.tool() decorated functions)
   - Async wrappers around sync BasecampClient methods
   - Authentication error handling with token expiration detection
   - Logging to both file and stderr (MCP best practice)

2. **basecamp_client.py** - Basecamp API client library
   - Synchronous HTTP client using requests library
   - Supports both Basic Auth and OAuth 2.0
   - Methods for all Basecamp 3 API endpoints
   - Automatic pagination handling for todos (get_todos method)

3. **oauth_app.py** - OAuth 2.0 flow handler
   - Web server for OAuth callback (port 8000)
   - Token exchange and storage
   - Token refresh mechanism

4. **token_storage.py** - Secure token persistence
   - JSON file storage (oauth_tokens.json)
   - Token expiration checking
   - Account ID tracking

5. **search_utils.py** - Search functionality
   - Cross-project search (projects, todos, messages)
   - Project-specific search
   - Result aggregation and filtering

6. **mcp_server_cli.py** - Legacy JSON-RPC server (deprecated, use basecamp_fastmcp.py)

### Authentication Flow

1. User runs `python oauth_app.py` → launches web server on localhost:8000
2. User visits http://localhost:8000 → redirected to Basecamp OAuth
3. User authorizes → Basecamp redirects back with code
4. Server exchanges code for access token → stores in oauth_tokens.json
5. MCP tools use token_storage.get_token() → get valid token
6. BasecampClient initialized with OAuth token → makes API calls

### MCP Tool Pattern

All MCP tools follow this pattern:
```python
@mcp.tool()
async def tool_name(param: str) -> Dict[str, Any]:
    """Tool description.

    Args:
        param: Parameter description
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        result = await _run_sync(client.sync_method, param)
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {"error": "OAuth token expired", "message": "..."}
        return {"error": "Execution error", "message": str(e)}
```

### Key Implementation Details

- **Async wrapper**: `_run_sync()` uses `anyio.to_thread.run_sync()` to run synchronous BasecampClient methods in thread pool
- **Pagination**: `get_todos()` in BasecampClient automatically fetches all pages by following `next` links in response headers
- **Native Search API**: Uses Basecamp's account-level search endpoint (`/search.json`) which searches across all content types (comments, messages, todos, cards, documents, campfire lines, etc.)
- **Error handling**: All tools check for 401 errors and expired tokens, returning user-friendly error messages
- **Logging**: Uses Python logging to both file (`basecamp_fastmcp.log`) and stderr (required for MCP debugging)

### Environment Variables

Required in `.env`:
- `BASECAMP_CLIENT_ID` - OAuth app client ID
- `BASECAMP_CLIENT_SECRET` - OAuth app client secret
- `BASECAMP_ACCOUNT_ID` - Basecamp account ID (visible in URL)
- `USER_AGENT` - Required by Basecamp API (format: "App Name (email)")

Optional (stored in oauth_tokens.json after OAuth):
- `access_token` - OAuth access token
- `refresh_token` - OAuth refresh token

## Testing Practices

- Tests use unittest framework
- Mock BasecampClient and API responses
- Test files in `tests/` directory
- Test MCP tool registration and parameter validation
- Integration tests require valid OAuth token

## Configuration Files

- `.env` - Environment variables (gitignored)
- `oauth_tokens.json` - OAuth tokens (gitignored, created by oauth_app.py)
- `~/.cursor/mcp.json` - Cursor MCP config (generated by generate_cursor_config.py)
- `~/Library/Application Support/Claude/claude_desktop_config.json` - Claude Desktop config (macOS)

## Search Functionality

The integration uses Basecamp's native search API for comprehensive search capabilities:

### Native Search Endpoint
- **URL**: `https://3.basecampapi.com/{account_id}/search.json?query={query}`
- **Scope**: Account-level (searches across all projects)
- **Content Types Searched**: Comments, Messages, Todos, Cards, Documents, Uploads, Campfire lines, and more

### Implementation Details
- `basecamp_client.py`: `search(query, page=1)` - low-level API method
- `search_utils.py`: `native_search(query, page=1, max_pages=None)` - handles pagination automatically
- `basecamp_fastmcp.py`:
  - `search_basecamp(query, max_pages=3)` - MCP tool for search
  - `global_search(query, max_pages=5)` - Alias for backward compatibility

### Pagination
- Each page returns up to 50 results
- `Link` header contains `rel="next"` if more pages available
- `max_pages` parameter limits total pages fetched (prevents long-running queries)

### Legacy Client-Side Search (Deprecated)
The `search_utils.py` file still contains client-side search methods (`search_projects()`, `search_todos()`, `search_messages()`) that download all data and filter locally. These are deprecated in favor of `native_search()` which is faster and searches campfire messages.

## Important Notes

- Always use `basecamp_fastmcp.py` as the MCP server (not mcp_server_cli.py)
- OAuth tokens expire - users must re-run `python oauth_app.py` when expired
- Basecamp API requires User-Agent header - must include app name and email
- MCP servers log to stderr (not stdout) - stdout is reserved for JSON-RPC protocol
- The server uses stdio transport - all tool input/output is JSON-RPC over stdin/stdout
- Virtual environment required for proper FastMCP isolation
- **Search uses native API** - project-level search endpoints don't exist, only account-level search works
