"""
Helper functions for standardizing API responses across MCP tools.

Provides:
- Text truncation (word-boundary aware)
- Pagination structure creation
- Response summarization for various Basecamp object types
"""

import re
from typing import Any, Dict, List, Optional


# Hard limits for different resource types
HARD_LIMITS = {
    "default": 16,      # projects, cards, documents, uploads, comments, campfire_lines, columns
    "todos": 64,        # todos, todolists, card_steps
}


def truncate_text_by_words(text: str, max_length: int = 150) -> str:
    """
    Truncate text to max_length characters at word boundary, adding ellipsis if truncated.

    Args:
        text: Text to truncate
        max_length: Maximum length in characters

    Returns:
        Truncated text with ellipsis if needed
    """
    if not text:
        return text

    text_str = str(text)
    if len(text_str) <= max_length:
        return text_str

    # Truncate to max_length
    truncated = text_str[:max_length]

    # Find last word boundary (space, punctuation, etc.)
    last_space = truncated.rfind(' ')
    if last_space > max_length * 0.8:  # Only use word boundary if it's not too far back
        truncated = truncated[:last_space]

    return truncated + "..."


def extract_names_from_people_list(people: List[Dict[str, Any]]) -> List[str]:
    """
    Extract just names from a list of person objects.

    Args:
        people: List of person objects with 'name' field

    Returns:
        List of names
    """
    if not people:
        return []
    return [person.get("name", "Unknown") for person in people if person]


def create_pagination_info(
    current_page: int,
    total_items: int,
    hard_limit: int,
    has_more: bool
) -> Dict[str, Any]:
    """
    Create standardized pagination object.

    Args:
        current_page: Current page number (1-based)
        total_items: Number of items on current page
        hard_limit: Maximum items per page
        has_more: Whether more pages exist

    Returns:
        Pagination dictionary
    """
    # Calculate showing range
    if total_items == 0:
        showing_results = "0"
    else:
        start_idx = 1
        end_idx = total_items
        showing_results = f"{start_idx}-{end_idx}"

    pagination = {
        "current_page": current_page,
        "results_per_page": hard_limit,
        "showing_results": showing_results,
        "total_results": total_items,
        "has_more": has_more
    }

    # Only calculate total_pages if we know there are more results
    if has_more:
        pagination["total_pages"] = "unknown (more pages available)"
    else:
        pagination["total_pages"] = current_page  # This is the last page

    return pagination


def chunk_content_by_words(
    content: str,
    offset: int = 0,
    length: int = 2000
) -> Dict[str, Any]:
    """
    Chunk content by word boundaries.

    Args:
        content: Full content text
        offset: Character offset to start from
        length: Maximum length of chunk in characters

    Returns:
        Dictionary with chunk info including total_length, offset, chunk, has_more
    """
    if not content:
        return {
            "total_length": 0,
            "offset": 0,
            "length": 0,
            "chunk": "",
            "has_more": False
        }

    content_str = str(content)
    total_length = len(content_str)

    # Validate offset
    if offset >= total_length:
        return {
            "total_length": total_length,
            "offset": offset,
            "length": 0,
            "chunk": "",
            "has_more": False
        }

    # Get chunk
    end_pos = min(offset + length, total_length)
    chunk = content_str[offset:end_pos]

    # If we're not at the end, try to break at word boundary
    if end_pos < total_length:
        last_space = chunk.rfind(' ')
        if last_space > length * 0.8:  # Only use word boundary if reasonable
            chunk = chunk[:last_space]
            end_pos = offset + last_space

    actual_length = len(chunk)
    has_more = end_pos < total_length

    return {
        "total_length": total_length,
        "offset": offset,
        "length": actual_length,
        "chunk": chunk,
        "has_more": has_more,
        "next_offset": end_pos if has_more else None
    }


def summarize_todo(todo: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a todo item."""
    summary = {
        "id": todo.get("id"),
        "content": truncate_text_by_words(todo.get("content", ""), 200),
        "status": todo.get("status"),
        "completed": todo.get("completed", False),
    }

    # Assignees - just names
    if todo.get("assignees"):
        summary["assignees"] = extract_names_from_people_list(todo["assignees"])

    # Dates
    if todo.get("due_on"):
        summary["due_on"] = todo["due_on"]
    if todo.get("starts_on"):
        summary["starts_on"] = todo["starts_on"]

    # Creator - just name
    if todo.get("creator"):
        summary["creator"] = todo["creator"].get("name")

    # Timestamps
    if todo.get("created_at"):
        summary["created_at"] = todo["created_at"]
    if todo.get("updated_at"):
        summary["updated_at"] = todo["updated_at"]

    # Parent info
    if todo.get("parent"):
        summary["parent"] = {
            "id": todo["parent"].get("id"),
            "title": truncate_text_by_words(todo["parent"].get("title", ""), 50),
            "type": todo["parent"].get("type")
        }

    # URLs
    if todo.get("url"):
        summary["api_url"] = todo["url"]
    if todo.get("app_url"):
        summary["web_url"] = todo["app_url"]

    return summary


def summarize_project(project: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a project."""
    summary = {
        "id": project.get("id"),
        "name": project.get("name"),
        "status": project.get("status"),
        "bookmarked": project.get("bookmarked", False),
    }

    # Purpose/description (truncated)
    if project.get("purpose"):
        summary["purpose"] = truncate_text_by_words(project.get("purpose"), 200)
    elif project.get("description"):
        summary["description"] = truncate_text_by_words(project.get("description"), 200)

    # Creator - just name
    if project.get("creator"):
        summary["creator"] = project["creator"].get("name")

    # Timestamps
    if project.get("created_at"):
        summary["created_at"] = project["created_at"]
    if project.get("updated_at"):
        summary["updated_at"] = project["updated_at"]

    # URLs
    if project.get("url"):
        summary["api_url"] = project["url"]
    if project.get("app_url"):
        summary["web_url"] = project["app_url"]

    return summary


def summarize_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a card."""
    summary = {
        "id": card.get("id"),
        "title": truncate_text_by_words(card.get("title", ""), 100),
        "status": card.get("status"),
    }

    # Content (truncated)
    if card.get("content"):
        summary["content"] = truncate_text_by_words(card.get("content"), 200)

    # Due date
    if card.get("due_on"):
        summary["due_on"] = card["due_on"]

    # Assignees - just names
    if card.get("assignees"):
        summary["assignees"] = extract_names_from_people_list(card["assignees"])

    # Creator - just name
    if card.get("creator"):
        summary["creator"] = card["creator"].get("name")

    # Timestamps
    if card.get("created_at"):
        summary["created_at"] = card["created_at"]

    # Parent/column info
    if card.get("parent"):
        summary["parent"] = {
            "id": card["parent"].get("id"),
            "title": truncate_text_by_words(card["parent"].get("title", ""), 50),
            "type": card["parent"].get("type")
        }

    # URLs
    if card.get("url"):
        summary["api_url"] = card["url"]
    if card.get("app_url"):
        summary["web_url"] = card["app_url"]

    return summary


def summarize_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a comment."""
    summary = {
        "id": comment.get("id"),
        "content": truncate_text_by_words(comment.get("content", ""), 200),
    }

    # Creator - just name
    if comment.get("creator"):
        summary["creator"] = comment["creator"].get("name")

    # Timestamp
    if comment.get("created_at"):
        summary["created_at"] = comment["created_at"]

    # Parent info
    if comment.get("parent"):
        summary["parent"] = {
            "id": comment["parent"].get("id"),
            "title": truncate_text_by_words(comment["parent"].get("title", ""), 50),
            "type": comment["parent"].get("type")
        }

    # URLs
    if comment.get("url"):
        summary["api_url"] = comment["url"]
    if comment.get("app_url"):
        summary["web_url"] = comment["app_url"]

    return summary


def summarize_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a document."""
    summary = {
        "id": document.get("id"),
        "title": truncate_text_by_words(document.get("title", ""), 100),
        "status": document.get("status"),
    }

    # Content preview (truncated)
    if document.get("content"):
        summary["content_preview"] = truncate_text_by_words(document.get("content"), 200)

    # Creator - just name
    if document.get("creator"):
        summary["creator"] = document["creator"].get("name")

    # Timestamps
    if document.get("created_at"):
        summary["created_at"] = document["created_at"]
    if document.get("updated_at"):
        summary["updated_at"] = document["updated_at"]

    # URLs
    if document.get("url"):
        summary["api_url"] = document["url"]
    if document.get("app_url"):
        summary["web_url"] = document["app_url"]

    return summary


def summarize_upload(upload: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize an upload."""
    summary = {
        "id": upload.get("id"),
        "filename": upload.get("filename"),
        "byte_size": upload.get("byte_size"),
        "content_type": upload.get("content_type"),
    }

    # Download URL
    if upload.get("download_url"):
        summary["download_url"] = upload["download_url"]

    # Creator - just name
    if upload.get("creator"):
        summary["creator"] = upload["creator"].get("name")

    # Timestamp
    if upload.get("created_at"):
        summary["created_at"] = upload["created_at"]

    # URLs
    if upload.get("url"):
        summary["api_url"] = upload["url"]
    if upload.get("app_url"):
        summary["web_url"] = upload["app_url"]

    return summary


def summarize_campfire_line(line: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a campfire chat line."""
    summary = {
        "id": line.get("id"),
        "content": truncate_text_by_words(line.get("content", ""), 200),
    }

    # Creator - just name
    if line.get("creator"):
        summary["creator"] = line["creator"].get("name")

    # Timestamp
    if line.get("created_at"):
        summary["created_at"] = line["created_at"]

    # URLs
    if line.get("url"):
        summary["api_url"] = line["url"]

    return summary


def summarize_todolist(todolist: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a todolist."""
    summary = {
        "id": todolist.get("id"),
        "name": todolist.get("name"),
        "status": todolist.get("status"),
    }

    # Description (truncated)
    if todolist.get("description"):
        summary["description"] = truncate_text_by_words(todolist.get("description"), 200)

    # Completed/total counts
    if "completed_count" in todolist:
        summary["completed_count"] = todolist["completed_count"]
    if "todos_count" in todolist:
        summary["todos_count"] = todolist["todos_count"]

    # Creator - just name
    if todolist.get("creator"):
        summary["creator"] = todolist["creator"].get("name")

    # Timestamps
    if todolist.get("created_at"):
        summary["created_at"] = todolist["created_at"]

    # URLs
    if todolist.get("url"):
        summary["api_url"] = todolist["url"]
    if todolist.get("app_url"):
        summary["web_url"] = todolist["app_url"]

    return summary


def summarize_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize a message."""
    summary = {
        "id": message.get("id"),
        "subject": truncate_text_by_words(message.get("subject", ""), 100),
        "status": message.get("status"),
    }

    # Content preview (truncated)
    if message.get("content"):
        summary["content_preview"] = truncate_text_by_words(message.get("content"), 200)

    # Creator - just name
    if message.get("creator"):
        summary["creator"] = message["creator"].get("name")

    # Timestamps
    if message.get("created_at"):
        summary["created_at"] = message["created_at"]
    if message.get("updated_at"):
        summary["updated_at"] = message["updated_at"]

    # URLs
    if message.get("url"):
        summary["api_url"] = message["url"]
    if message.get("app_url"):
        summary["web_url"] = message["app_url"]

    return summary
