#!/usr/bin/env python3
"""
FastMCP server for Basecamp integration.

This server implements the MCP (Model Context Protocol) using the official
Anthropic FastMCP framework, replacing the custom JSON-RPC implementation.
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional
import anyio
import httpx
from mcp.server.fastmcp import FastMCP

# Import existing business logic
from basecamp_client import BasecampClient
from search_utils import BasecampSearch
import token_storage
from dotenv import load_dotenv

# Determine project root (directory containing this script)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DOTENV_PATH = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(DOTENV_PATH)

# Set up logging to file AND stderr (following MCP best practices)
LOG_FILE_PATH = os.path.join(PROJECT_ROOT, 'basecamp_fastmcp.log')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler(sys.stderr)  # Critical: log to stderr, not stdout
    ]
)
logger = logging.getLogger('basecamp_fastmcp')

# Initialize FastMCP server
mcp = FastMCP("basecamp")

# Auth helper functions (reused from original server)
def _get_basecamp_client() -> Optional[BasecampClient]:
    """Get authenticated Basecamp client (sync version from original server).

    Automatically refreshes expired tokens if a refresh token is available.
    """
    try:
        # Use ensure_valid_token which auto-refreshes if needed
        token_data = token_storage.ensure_valid_token()
        logger.debug(f"Token data retrieved: token_exists={token_data is not None}, has_access_token={bool(token_data.get('access_token') if token_data else False)}")

        if not token_data or not token_data.get('access_token'):
            logger.error("No valid OAuth token available (refresh may have failed)")
            return None

        # Get account_id from token data first, then fall back to env var
        account_id = token_data.get('account_id') or os.getenv('BASECAMP_ACCOUNT_ID')
        user_agent = os.getenv('USER_AGENT') or "Basecamp MCP Server (cursor@example.com)"

        if not account_id:
            logger.error(f"Missing account_id. Token data: {token_data}, Env BASECAMP_ACCOUNT_ID: {os.getenv('BASECAMP_ACCOUNT_ID')}")
            return None

        logger.debug(f"Creating Basecamp client with account_id: {account_id}, user_agent: {user_agent}")

        return BasecampClient(
            access_token=token_data['access_token'],
            account_id=account_id,
            user_agent=user_agent,
            auth_mode='oauth'
        )
    except Exception as e:
        logger.error(f"Error creating Basecamp client: {e}")
        return None

def _get_auth_error_response() -> Dict[str, Any]:
    """Return consistent auth error response."""
    # At this point, ensure_valid_token already tried to refresh
    return {
        "error": "Authentication required",
        "message": "Please authenticate with Basecamp first. Visit http://localhost:8000 to log in. If you were previously authenticated, your token may have expired and could not be refreshed."
    }

async def _run_sync(func, *args, **kwargs):
    """Wrapper to run synchronous functions in thread pool."""
    return await anyio.to_thread.run_sync(func, *args, **kwargs)

# Core MCP Tools - Starting with essential ones from original server

@mcp.tool()
async def get_projects(
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get all Basecamp projects.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_project

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        all_projects = await _run_sync(client.get_projects)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_projects = all_projects[start_idx:end_idx]
        has_more = end_idx < len(all_projects)

        if not raw_response:
            page_projects = [summarize_project(p) for p in page_projects]

        pagination = create_pagination_info(page, len(page_projects), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "projects": page_projects,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting projects: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_project(project_id: str) -> Dict[str, Any]:
    """Get details for a specific project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        project = await _run_sync(client.get_project, project_id)
        return {
            "status": "success",
            "project": project
        }
    except Exception as e:
        logger.error(f"Error getting project {project_id}: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def search_basecamp(query: str, page: Optional[int] = 1, max_results: Optional[int] = 16) -> Dict[str, Any]:
    """Search across all Basecamp content using native search API.

    Searches across:
    - Comments
    - Messages
    - Todos
    - Cards
    - Documents
    - Uploads
    - Campfire/chat messages
    - And more...

    Returns summarized results with id, type, title, preview (truncated), and URLs.
    Use get_comments, get_card, get_document, etc. with the returned IDs to fetch full details.

    Results are paginated with a hard limit of 16 results per page.

    Args:
        query: Search query string
        page: Page number to fetch (default: 1, 1-based)
        max_results: Maximum number of results to return (default: 16, hard limit: 16)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        search = BasecampSearch(client=client)
        # Use native search API which is much faster and more comprehensive
        result = await _run_sync(
            lambda: search.native_search(query, page=page, max_results=max_results)
        )

        # Return the full result which includes status, query, pagination, results, note, and optional warning
        return result
    except Exception as e:
        logger.error(f"Error searching Basecamp: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todolists(
    project_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 64,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get todo lists for a project.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: The project ID
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 64, hard limit: 64)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_todolist

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["todos"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        all_todolists = await _run_sync(client.get_todolists, project_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_todolists = all_todolists[start_idx:end_idx]
        has_more = end_idx < len(all_todolists)

        if not raw_response:
            page_todolists = [summarize_todolist(tl) for tl in page_todolists]

        pagination = create_pagination_info(page, len(page_todolists), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "todolists": page_todolists,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting todolists: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todos(
    project_id: str,
    todolist_id: str,
    completed: Optional[bool] = None,
    page: Optional[int] = 1,
    max_results: Optional[int] = 64,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get todos from a todo list.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: Project ID
        todolist_id: The todo list ID
        completed: Filter by completion status. True = only completed, False = only incomplete,
                   None (default) = incomplete only (Basecamp API default).
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 64, hard limit: 64)
        raw_response: If True, return complete API response without summarization (default: False)
                     WARNING: Only use when you need complete object details
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_todo

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        # Hard limit enforcement
        HARD_LIMIT = HARD_LIMITS["todos"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        # Get all todos (client handles pagination internally)
        all_todos = await _run_sync(lambda: client.get_todos(project_id, todolist_id, completed=completed))

        # Calculate pagination
        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_todos = all_todos[start_idx:end_idx]
        has_more = end_idx < len(all_todos)

        # Summarize if not raw response
        if not raw_response:
            page_todos = [summarize_todo(todo) for todo in page_todos]

        # Create response
        pagination = create_pagination_info(page, len(page_todos), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "todos": page_todos,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning

        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting todos: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todo(todo_id: str, raw_response: Optional[bool] = False) -> Dict[str, Any]:
    """Get a specific todo by its ID.

    Fetches a single todo item directly by ID, regardless of completion status.
    Useful when you know the todo ID and want to inspect its details or comments.

    Args:
        todo_id: The todo ID
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import summarize_todo

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todo = await _run_sync(client.get_todo, todo_id)

        if not raw_response:
            todo = summarize_todo(todo)

        return {
            "status": "success",
            "todo": todo
        }

    except Exception as e:
        logger.error(f"Error getting todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_todolist(todolist_id: str, raw_response: Optional[bool] = False) -> Dict[str, Any]:
    """Get a specific todolist by its ID.

    Fetches a single todolist directly by ID. Useful when you know the todolist ID
    and want to inspect its details without fetching all todolists for a project.

    Args:
        todolist_id: The todolist ID
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import summarize_todolist

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        todolist = await _run_sync(client.get_todolist, todolist_id)

        if not raw_response:
            todolist = summarize_todolist(todolist)

        return {
            "status": "success",
            "todolist": todolist
        }

    except Exception as e:
        logger.error(f"Error getting todolist: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_todo(project_id: str, todolist_id: str, content: str,
                     description: Optional[str] = None, 
                     assignee_ids: Optional[List[str]] = None,
                     completion_subscriber_ids: Optional[List[str]] = None, 
                     notify: bool = False, 
                     due_on: Optional[str] = None, 
                     starts_on: Optional[str] = None) -> Dict[str, Any]:
    """Create a new todo item in a todo list.
    
    Args:
        project_id: Project ID
        todolist_id: The todo list ID
        content: The todo item's text (required)
        description: HTML description of the todo
        assignee_ids: List of person IDs to assign
        completion_subscriber_ids: List of person IDs to notify on completion
        notify: Whether to notify assignees
        due_on: Due date in YYYY-MM-DD format
        starts_on: Start date in YYYY-MM-DD format
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        # Use lambda to properly handle keyword arguments
        todo = await _run_sync(
            lambda: client.create_todo(
                project_id, todolist_id, content,
                description=description,
                assignee_ids=assignee_ids,
                completion_subscriber_ids=completion_subscriber_ids,
                notify=notify,
                due_on=due_on,
                starts_on=starts_on
            )
        )
        return {
            "status": "success",
            "todo": todo,
            "message": f"Todo '{content}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_todo(project_id: str, todo_id: str, 
                     content: Optional[str] = None,
                     description: Optional[str] = None, 
                     assignee_ids: Optional[List[str]] = None,
                     completion_subscriber_ids: Optional[List[str]] = None,
                     notify: Optional[bool] = None,
                     due_on: Optional[str] = None, 
                     starts_on: Optional[str] = None) -> Dict[str, Any]:
    """Update an existing todo item.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
        content: The todo item's text
        description: HTML description of the todo
        assignee_ids: List of person IDs to assign
        completion_subscriber_ids: List of person IDs to notify on completion
        due_on: Due date in YYYY-MM-DD format
        starts_on: Start date in YYYY-MM-DD format
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        # Guard against no-op updates
        if all(v is None for v in [content, description, assignee_ids,
                                   completion_subscriber_ids, notify,
                                   due_on, starts_on]):
            return {
                "error": "Invalid input",
                "message": "At least one field to update must be provided"
            }
        # Use lambda to properly handle keyword arguments
        todo = await _run_sync(
            lambda: client.update_todo(
                project_id, todo_id,
                content=content,
                description=description,
                assignee_ids=assignee_ids,
                completion_subscriber_ids=completion_subscriber_ids,
                notify=notify,
                due_on=due_on,
                starts_on=starts_on
            )
        )
        return {
            "status": "success",
            "todo": todo,
            "message": "Todo updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Delete a todo item.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.delete_todo, project_id, todo_id)
        return {
            "status": "success",
            "message": "Todo deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Mark a todo item as complete.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        completion = await _run_sync(client.complete_todo, project_id, todo_id)
        return {
            "status": "success",
            "completion": completion,
            "message": "Todo marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def uncomplete_todo(project_id: str, todo_id: str) -> Dict[str, Any]:
    """Mark a todo item as incomplete.
    
    Args:
        project_id: Project ID
        todo_id: The todo ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_todo, project_id, todo_id)
        return {
            "status": "success",
            "message": "Todo marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting todo: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def global_search(query: str, page: Optional[int] = 1, max_results: Optional[int] = 16) -> Dict[str, Any]:
    """Search across all Basecamp content using native search API.

    This is an alias for search_basecamp() for backward compatibility.
    Searches across all content types including comments, messages, todos,
    cards, documents, uploads, and campfire/chat messages.

    Returns summarized results with id, type, title, preview (truncated), and URLs.
    Use get_comments, get_card, get_document, etc. with the returned IDs to fetch full details.

    Results are paginated with a hard limit of 16 results per page.

    Args:
        query: Search query string
        page: Page number to fetch (default: 1, 1-based)
        max_results: Maximum number of results to return (default: 16, hard limit: 16)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        search = BasecampSearch(client=client)
        # Use native search API which is much faster and more comprehensive
        result = await _run_sync(
            lambda: search.native_search(query, page=page, max_results=max_results)
        )

        # Return the full result which includes status, query, pagination, results, note, and optional warning
        return result
    except Exception as e:
        logger.error(f"Error in global search: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_comments(
    recording_id: str,
    project_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get comments for a Basecamp item.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        recording_id: The item ID
        project_id: The project ID
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_comment

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        all_comments = await _run_sync(client.get_comments, project_id, recording_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_comments = all_comments[start_idx:end_idx]
        has_more = end_idx < len(all_comments)

        if not raw_response:
            page_comments = [summarize_comment(c) for c in page_comments]

        pagination = create_pagination_info(page, len(page_comments), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "comments": page_comments,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting comments: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_comment(recording_id: str, project_id: str, content: str) -> Dict[str, Any]:
    """Create a comment on a Basecamp item.

    Args:
        recording_id: The item ID
        project_id: The project ID
        content: The comment content in HTML format
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        comment = await _run_sync(client.create_comment, recording_id, project_id, content)
        return {
            "status": "success",
            "comment": comment,
            "message": "Comment created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating comment: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again.",
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_campfire_lines(
    project_id: str,
    campfire_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get recent messages from a Basecamp campfire (chat room).

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: The project ID
        campfire_id: The campfire/chat room ID
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_campfire_line

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        all_lines = await _run_sync(client.get_campfire_lines, project_id, campfire_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_lines = all_lines[start_idx:end_idx]
        has_more = end_idx < len(all_lines)

        if not raw_response:
            page_lines = [summarize_campfire_line(line) for line in page_lines]

        pagination = create_pagination_info(page, len(page_lines), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "campfire_lines": page_lines,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting campfire lines: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_messages(
    project_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get messages from a project's message board.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: The project ID
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_message

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page to prevent excessive token usage."
            max_results = HARD_LIMIT
            logger.warning(warning)

        all_messages = await _run_sync(client.get_messages, project_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_messages = all_messages[start_idx:end_idx]
        has_more = end_idx < len(all_messages)

        if not raw_response:
            page_messages = [summarize_message(msg) for msg in page_messages]

        pagination = create_pagination_info(page, len(page_messages), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "messages": page_messages,
            "note": "Results are summarized. Use raw_response=True to get complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" To see more results, request page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_message(
    project_id: str,
    message_id: str,
    content_offset: Optional[int] = 0,
    content_length: Optional[int] = 2000,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get a single message with optional content chunking.

    For large messages, use content_offset and content_length to retrieve content in chunks.

    Args:
        project_id: Project ID
        message_id: Message ID
        content_offset: Character offset to start from (default: 0)
        content_length: Maximum length of content chunk in characters (default: 2000)
        raw_response: If True, return complete API response (default: False)
    """
    from response_helpers import chunk_content_by_words

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        message = await _run_sync(client.get_message, project_id, message_id)

        # If raw response requested, return as-is
        if raw_response:
            return {
                "status": "success",
                "message": message
            }

        # Extract and chunk content
        content = message.get("content", "")
        content_info = chunk_content_by_words(content, content_offset, content_length)

        # Build summarized response with chunked content
        result = {
            "status": "success",
            "message": {
                "id": message.get("id"),
                "subject": message.get("subject"),
                "status": message.get("status"),
                "content_info": content_info,
                "creator": message.get("creator", {}).get("name") if message.get("creator") else None,
                "created_at": message.get("created_at"),
                "updated_at": message.get("updated_at"),
                "url": message.get("url"),
                "app_url": message.get("app_url")
            },
            "note": "Content is chunked. Use content_offset and content_length to navigate. Use raw_response=True for complete message."
        }

        if content_info.get("has_more"):
            next_offset = content_info.get("next_offset")
            result["note"] += f" More content available at offset={next_offset}."

        return result

    except Exception as e:
        logger.error(f"Error getting message: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card_tables(project_id: str) -> Dict[str, Any]:
    """Get all card tables for a project.

    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card_tables = await _run_sync(client.get_card_tables, project_id)
        return {
            "status": "success",
            "card_tables": card_tables,
            "count": len(card_tables)
        }
    except Exception as e:
        logger.error(f"Error getting card tables: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card_table(project_id: str) -> Dict[str, Any]:
    """Get the card table details for a project.
    
    Args:
        project_id: The project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card_table = await _run_sync(client.get_card_table, project_id)
        card_table_details = await _run_sync(client.get_card_table_details, project_id, card_table['id'])
        return {
            "status": "success",
            "card_table": card_table_details
        }
    except Exception as e:
        logger.error(f"Error getting card table: {e}")
        error_msg = str(e)
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "status": "error",
            "message": f"Error getting card table: {error_msg}",
            "debug": error_msg
        }

@mcp.tool()
async def get_columns(project_id: str, card_table_id: str) -> Dict[str, Any]:
    """Get all columns in a card table.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        columns = await _run_sync(client.get_columns, project_id, card_table_id)
        return {
            "status": "success",
            "columns": columns,
            "count": len(columns)
        }
    except Exception as e:
        logger.error(f"Error getting columns: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_cards(
    project_id: str,
    column_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get all cards in a column.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: The project ID
        column_id: The column ID
        page: Page number (default: 1, 1-based)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response without summarization (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_card

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, but limited to maximum of {HARD_LIMIT} per page."
            max_results = HARD_LIMIT

        all_cards = await _run_sync(client.get_cards, project_id, column_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_cards = all_cards[start_idx:end_idx]
        has_more = end_idx < len(all_cards)

        if not raw_response:
            page_cards = [summarize_card(c) for c in page_cards]

        pagination = create_pagination_info(page, len(page_cards), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "cards": page_cards,
            "note": "Results are summarized. Use raw_response=True for complete details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" More on page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting cards: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "status": "error",
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call."
            }
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def create_card(project_id: str, column_id: str, title: str, content: Optional[str] = None, due_on: Optional[str] = None, notify: bool = False) -> Dict[str, Any]:
    """Create a new card in a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        title: The card title
        content: Optional card content/description
        due_on: Optional due date (ISO 8601 format)
        notify: Whether to notify assignees (default: false)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card = await _run_sync(client.create_card, project_id, column_id, title, content, due_on, notify)
        return {
            "status": "success",
            "card": card,
            "message": f"Card '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Get details for a specific column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.get_column, project_id, column_id)
        return {
            "status": "success",
            "column": column
        }
    except Exception as e:
        logger.error(f"Error getting column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_column(project_id: str, card_table_id: str, title: str) -> Dict[str, Any]:
    """Create a new column in a card table.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
        title: The column title
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.create_column, project_id, card_table_id, title)
        return {
            "status": "success",
            "column": column,
            "message": f"Column '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def move_card(project_id: str, card_id: str, column_id: str) -> Dict[str, Any]:
    """Move a card to a new column.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        column_id: The destination column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.move_card, project_id, card_id, column_id)
        return {
            "status": "success",
            "message": f"Card moved to column {column_id}"
        }
    except Exception as e:
        logger.error(f"Error moving card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_card(project_id: str, card_id: str) -> Dict[str, Any]:
    """Mark a card as complete.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.complete_card, project_id, card_id)
        return {
            "status": "success",
            "message": "Card marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card(
    project_id: str,
    card_id: str,
    content_offset: Optional[int] = 0,
    content_length: Optional[int] = 2000,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get details for a specific card with optional content chunking.

    For cards with large content, use content_offset and content_length to retrieve content in chunks.

    Args:
        project_id: The project ID
        card_id: The card ID
        content_offset: Character offset to start from (default: 0)
        content_length: Maximum length of content chunk in characters (default: 2000)
        raw_response: If True, return complete API response (default: False)
    """
    from response_helpers import chunk_content_by_words, extract_names_from_people_list

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        card = await _run_sync(client.get_card, project_id, card_id)

        # If raw response requested, return as-is
        if raw_response:
            return {
                "status": "success",
                "card": card
            }

        # Extract and chunk content
        content = card.get("content", "")
        content_info = chunk_content_by_words(content, content_offset, content_length)

        # Build summarized response with chunked content
        result = {
            "status": "success",
            "card": {
                "id": card.get("id"),
                "title": card.get("title"),
                "status": card.get("status"),
                "content_info": content_info,
                "due_on": card.get("due_on"),
                "assignees": extract_names_from_people_list(card.get("assignees", [])),
                "creator": card.get("creator", {}).get("name") if card.get("creator") else None,
                "created_at": card.get("created_at"),
                "updated_at": card.get("updated_at"),
                "url": card.get("url"),
                "app_url": card.get("app_url")
            },
            "note": "Content is chunked. Use content_offset and content_length to navigate. Use raw_response=True for complete card."
        }

        if content_info.get("has_more"):
            next_offset = content_info.get("next_offset")
            result["note"] += f" More content available at offset={next_offset}."

        return result

    except Exception as e:
        logger.error(f"Error getting card: {e}")
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def update_card(project_id: str, card_id: str, title: Optional[str] = None, content: Optional[str] = None, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Update a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        title: The new card title
        content: The new card content/description
        due_on: Due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the card
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        card = await _run_sync(client.update_card, project_id, card_id, title, content, due_on, assignee_ids)
        return {
            "status": "success",
            "card": card,
            "message": "Card updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_daily_check_ins(project_id: str, page: Optional[int] = None) -> Dict[str, Any]:
    """Get project's daily checking questionnaire.
    
    Args:
        project_id: The project ID
        page: Page number paginated response
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        if page is not None and not isinstance(page, int):
            page = 1
        answers = await _run_sync(client.get_daily_check_ins, project_id, page=page or 1)
        return {
            "status": "success",
            "campfire_lines": answers,
            "count": len(answers)
        }
    except Exception as e:
        logger.error(f"Error getting daily check ins: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_question_answers(project_id: str, question_id: str, page: Optional[int] = None) -> Dict[str, Any]:
    """Get answers on daily check-in question.
    
    Args:
        project_id: The project ID
        question_id: The question ID
        page: Page number paginated response
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        if page is not None and not isinstance(page, int):
            page = 1
        answers = await _run_sync(client.get_question_answers, project_id, question_id, page=page or 1)
        return {
            "status": "success",
            "campfire_lines": answers,
            "count": len(answers)
        }
    except Exception as e:
        logger.error(f"Error getting question answers: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Column Management Tools
@mcp.tool()
async def update_column(project_id: str, column_id: str, title: str) -> Dict[str, Any]:
    """Update a column title.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        title: The new column title
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.update_column, project_id, column_id, title)
        return {
            "status": "success",
            "column": column,
            "message": "Column updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def move_column(project_id: str, card_table_id: str, column_id: str, position: int) -> Dict[str, Any]:
    """Move a column to a new position.
    
    Args:
        project_id: The project ID
        card_table_id: The card table ID
        column_id: The column ID
        position: The new 1-based position
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.move_column, project_id, column_id, position, card_table_id)
        return {
            "status": "success",
            "message": f"Column moved to position {position}"
        }
    except Exception as e:
        logger.error(f"Error moving column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_column_color(project_id: str, column_id: str, color: str) -> Dict[str, Any]:
    """Update a column color.
    
    Args:
        project_id: The project ID
        column_id: The column ID
        color: The hex color code (e.g., #FF0000)
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        column = await _run_sync(client.update_column_color, project_id, column_id, color)
        return {
            "status": "success",
            "column": column,
            "message": f"Column color updated to {color}"
        }
    except Exception as e:
        logger.error(f"Error updating column color: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def put_column_on_hold(project_id: str, column_id: str) -> Dict[str, Any]:
    """Put a column on hold (freeze work).
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.put_column_on_hold, project_id, column_id)
        return {
            "status": "success",
            "message": "Column put on hold"
        }
    except Exception as e:
        logger.error(f"Error putting column on hold: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def remove_column_hold(project_id: str, column_id: str) -> Dict[str, Any]:
    """Remove hold from a column (unfreeze work).
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.remove_column_hold, project_id, column_id)
        return {
            "status": "success",
            "message": "Column hold removed"
        }
    except Exception as e:
        logger.error(f"Error removing column hold: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def watch_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Subscribe to notifications for changes in a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.watch_column, project_id, column_id)
        return {
            "status": "success",
            "message": "Column notifications enabled"
        }
    except Exception as e:
        logger.error(f"Error watching column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def unwatch_column(project_id: str, column_id: str) -> Dict[str, Any]:
    """Unsubscribe from notifications for a column.
    
    Args:
        project_id: The project ID
        column_id: The column ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.unwatch_column, project_id, column_id)
        return {
            "status": "success",
            "message": "Column notifications disabled"
        }
    except Exception as e:
        logger.error(f"Error unwatching column: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# More Card Management Tools  
@mcp.tool()
async def uncomplete_card(project_id: str, card_id: str) -> Dict[str, Any]:
    """Mark a card as incomplete.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_card, project_id, card_id)
        return {
            "status": "success",
            "message": "Card marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting card: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Card Steps (Sub-tasks) Management
@mcp.tool()
async def get_card_steps(project_id: str, card_id: str) -> Dict[str, Any]:
    """Get all steps (sub-tasks) for a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        steps = await _run_sync(client.get_card_steps, project_id, card_id)
        return {
            "status": "success",
            "steps": steps,
            "count": len(steps)
        }
    except Exception as e:
        logger.error(f"Error getting card steps: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_card_step(project_id: str, card_id: str, title: str, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a new step (sub-task) for a card.
    
    Args:
        project_id: The project ID
        card_id: The card ID
        title: The step title
        due_on: Optional due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the step
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.create_card_step, project_id, card_id, title, due_on, assignee_ids)
        return {
            "status": "success",
            "step": step,
            "message": f"Step '{title}' created successfully"
        }
    except Exception as e:
        logger.error(f"Error creating card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Get details for a specific card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.get_card_step, project_id, step_id)
        return {
            "status": "success",
            "step": step
        }
    except Exception as e:
        logger.error(f"Error getting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_card_step(project_id: str, step_id: str, title: Optional[str] = None, due_on: Optional[str] = None, assignee_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Update a card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
        title: The step title
        due_on: Due date (ISO 8601 format)
        assignee_ids: Array of person IDs to assign to the step
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        step = await _run_sync(client.update_card_step, project_id, step_id, title, due_on, assignee_ids)
        return {
            "status": "success",
            "step": step,
            "message": f"Step updated successfully"
        }
    except Exception as e:
        logger.error(f"Error updating card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Delete a card step.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.delete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def complete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Mark a card step as complete.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.complete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step marked as complete"
        }
    except Exception as e:
        logger.error(f"Error completing card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def uncomplete_card_step(project_id: str, step_id: str) -> Dict[str, Any]:
    """Mark a card step as incomplete.
    
    Args:
        project_id: The project ID
        step_id: The step ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.uncomplete_card_step, project_id, step_id)
        return {
            "status": "success",
            "message": "Step marked as incomplete"
        }
    except Exception as e:
        logger.error(f"Error uncompleting card step: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Attachments, Events, and Webhooks
@mcp.tool()
async def create_attachment(file_path: str, name: str, content_type: Optional[str] = None) -> Dict[str, Any]:
    """Upload a file as an attachment.
    
    Args:
        file_path: Local path to file
        name: Filename for Basecamp
        content_type: MIME type
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        result = await _run_sync(client.create_attachment, file_path, name, content_type or "application/octet-stream")
        return {
            "status": "success",
            "attachment": result
        }
    except Exception as e:
        logger.error(f"Error creating attachment: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_events(project_id: str, recording_id: str) -> Dict[str, Any]:
    """Get events for a recording.
    
    Args:
        project_id: Project ID
        recording_id: Recording ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        events = await _run_sync(client.get_events, project_id, recording_id)
        return {
            "status": "success",
            "events": events,
            "count": len(events)
        }
    except Exception as e:
        logger.error(f"Error getting events: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def get_webhooks(project_id: str) -> Dict[str, Any]:
    """List webhooks for a project.
    
    Args:
        project_id: Project ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        hooks = await _run_sync(client.get_webhooks, project_id)
        return {
            "status": "success",
            "webhooks": hooks,
            "count": len(hooks)
        }
    except Exception as e:
        logger.error(f"Error getting webhooks: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def create_webhook(project_id: str, payload_url: str, types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a webhook.
    
    Args:
        project_id: Project ID
        payload_url: Payload URL
        types: Event types
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        hook = await _run_sync(client.create_webhook, project_id, payload_url, types)
        return {
            "status": "success",
            "webhook": hook
        }
    except Exception as e:
        logger.error(f"Error creating webhook: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def delete_webhook(project_id: str, webhook_id: str) -> Dict[str, Any]:
    """Delete a webhook.
    
    Args:
        project_id: Project ID
        webhook_id: Webhook ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.delete_webhook, project_id, webhook_id)
        return {
            "status": "success",
            "message": "Webhook deleted"
        }
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Document Management
@mcp.tool()
async def get_documents(
    project_id: str,
    vault_id: str,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """List documents in a vault.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: Project ID
        vault_id: Vault ID
        page: Page number (default: 1)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_document

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, limited to {HARD_LIMIT}."
            max_results = HARD_LIMIT

        all_docs = await _run_sync(client.get_documents, project_id, vault_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_docs = all_docs[start_idx:end_idx]
        has_more = end_idx < len(all_docs)

        if not raw_response:
            page_docs = [summarize_document(d) for d in page_docs]

        pagination = create_pagination_info(page, len(page_docs), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "documents": page_docs,
            "note": "Summarized. Use raw_response=True for full details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" More on page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting documents: {e}")
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def get_document(
    project_id: str,
    document_id: str,
    content_offset: Optional[int] = 0,
    content_length: Optional[int] = 2000,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """Get a single document with optional content chunking.

    For large documents, use content_offset and content_length to retrieve content in chunks.

    Args:
        project_id: Project ID
        document_id: Document ID
        content_offset: Character offset to start from (default: 0)
        content_length: Maximum length of content chunk in characters (default: 2000)
        raw_response: If True, return complete API response (default: False)
    """
    from response_helpers import chunk_content_by_words

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        doc = await _run_sync(client.get_document, project_id, document_id)

        # If raw response requested, return as-is
        if raw_response:
            return {
                "status": "success",
                "document": doc
            }

        # Extract and chunk content
        content = doc.get("content", "")
        content_info = chunk_content_by_words(content, content_offset, content_length)

        # Build summarized response with chunked content
        result = {
            "status": "success",
            "document": {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "status": doc.get("status"),
                "content_info": content_info,
                "creator": doc.get("creator", {}).get("name") if doc.get("creator") else None,
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
                "url": doc.get("url"),
                "app_url": doc.get("app_url")
            },
            "note": "Content is chunked. Use content_offset and content_length to navigate. Use raw_response=True for complete document."
        }

        if content_info.get("has_more"):
            next_offset = content_info.get("next_offset")
            result["note"] += f" More content available at offset={next_offset}."

        return result

    except Exception as e:
        logger.error(f"Error getting document: {e}")
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def create_document(project_id: str, vault_id: str, title: str, content: str) -> Dict[str, Any]:
    """Create a document in a vault.
    
    Args:
        project_id: Project ID
        vault_id: Vault ID
        title: Document title
        content: Document HTML content
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        doc = await _run_sync(client.create_document, project_id, vault_id, title, content)
        return {
            "status": "success",
            "document": doc
        }
    except Exception as e:
        logger.error(f"Error creating document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def update_document(project_id: str, document_id: str, title: Optional[str] = None, content: Optional[str] = None) -> Dict[str, Any]:
    """Update a document.
    
    Args:
        project_id: Project ID
        document_id: Document ID
        title: New title
        content: New HTML content
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        doc = await _run_sync(client.update_document, project_id, document_id, title, content)
        return {
            "status": "success",
            "document": doc
        }
    except Exception as e:
        logger.error(f"Error updating document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

@mcp.tool()
async def trash_document(project_id: str, document_id: str) -> Dict[str, Any]:
    """Move a document to trash.
    
    Args:
        project_id: Project ID
        document_id: Document ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        await _run_sync(client.trash_document, project_id, document_id)
        return {
            "status": "success",
            "message": "Document trashed"
        }
    except Exception as e:
        logger.error(f"Error trashing document: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# Upload Management
@mcp.tool()
async def get_uploads(
    project_id: str,
    vault_id: Optional[str] = None,
    page: Optional[int] = 1,
    max_results: Optional[int] = 16,
    raw_response: Optional[bool] = False
) -> Dict[str, Any]:
    """List uploads in a project or vault.

    Results are paginated and summarized by default for optimal token usage.

    Args:
        project_id: Project ID
        vault_id: Optional vault ID to limit to specific vault
        page: Page number (default: 1)
        max_results: Maximum results per page (default: 16, hard limit: 16)
        raw_response: If True, return complete API response (default: False)
    """
    from response_helpers import HARD_LIMITS, create_pagination_info, summarize_upload

    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()

    try:
        HARD_LIMIT = HARD_LIMITS["default"]
        warning = None

        if max_results > HARD_LIMIT:
            warning = f"Requested {max_results} results, limited to {HARD_LIMIT}."
            max_results = HARD_LIMIT

        all_uploads = await _run_sync(client.get_uploads, project_id, vault_id)

        start_idx = (page - 1) * max_results
        end_idx = start_idx + max_results
        page_uploads = all_uploads[start_idx:end_idx]
        has_more = end_idx < len(all_uploads)

        if not raw_response:
            page_uploads = [summarize_upload(u) for u in page_uploads]

        pagination = create_pagination_info(page, len(page_uploads), HARD_LIMIT, has_more)

        result = {
            "status": "success",
            "pagination": pagination,
            "uploads": page_uploads,
            "note": "Summarized. Use raw_response=True for full details."
        }

        if warning:
            result["warning"] = warning
        if has_more:
            result["note"] += f" More on page={page + 1}."

        return result

    except Exception as e:
        logger.error(f"Error getting uploads: {e}")
        return {"status": "error", "error": str(e)}

@mcp.tool()
async def get_upload(project_id: str, upload_id: str) -> Dict[str, Any]:
    """Get details for a specific upload.
    
    Args:
        project_id: Project ID
        upload_id: Upload ID
    """
    client = _get_basecamp_client()
    if not client:
        return _get_auth_error_response()
    
    try:
        upload = await _run_sync(client.get_upload, project_id, upload_id)
        return {
            "status": "success",
            "upload": upload
        }
    except Exception as e:
        logger.error(f"Error getting upload: {e}")
        if "401" in str(e) and "expired" in str(e).lower():
            return {
                "error": "OAuth token expired",
                "message": "Your Basecamp OAuth token expired during the API call. Please re-authenticate by visiting http://localhost:8000 and completing the OAuth flow again."
            }
        return {
            "error": "Execution error",
            "message": str(e)
        }

# 🎉 COMPLETE FastMCP server with ALL 46 Basecamp tools migrated!

if __name__ == "__main__":
    logger.info("Starting Basecamp FastMCP server")
    # Run using official MCP stdio transport
    mcp.run(transport='stdio') 