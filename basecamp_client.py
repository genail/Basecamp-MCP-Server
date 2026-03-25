import json
import logging
import os

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class BasecampError(Exception):
    """Base exception for Basecamp API errors."""
    def __init__(self, message, status_code=None):
        self.status_code = status_code
        super().__init__(message)


class AuthenticationError(BasecampError):
    """401 Unauthorized — token expired or invalid."""
    pass


class ForbiddenError(BasecampError):
    """403 Forbidden — insufficient permissions."""
    pass


class NotFoundError(BasecampError):
    """404 Not Found — resource does not exist."""
    pass


class RateLimitError(BasecampError):
    """429 Too Many Requests — rate limited by Basecamp."""
    def __init__(self, message, retry_after=None, status_code=429):
        self.retry_after = retry_after
        super().__init__(message, status_code=status_code)


class ServerError(BasecampError):
    """5xx — Basecamp server-side error."""
    pass


# Safety limit for pagination loops
MAX_PAGES = 500

# HTTP request timeout in seconds
REQUEST_TIMEOUT = 30


class BasecampClient:
    """
    Client for interacting with Basecamp 3 API using Basic Authentication or OAuth 2.0.
    """

    def __init__(self, username=None, password=None, account_id=None, user_agent=None,
                 access_token=None, auth_mode="basic"):
        """
        Initialize the Basecamp client with credentials.

        Args:
            username (str, optional): Basecamp username (email) for Basic Auth
            password (str, optional): Basecamp password for Basic Auth
            account_id (str, optional): Basecamp account ID
            user_agent (str, optional): User agent for API requests
            access_token (str, optional): OAuth access token for OAuth Auth
            auth_mode (str, optional): Authentication mode ('basic' or 'oauth')
        """
        # Load environment variables if not provided directly
        load_dotenv()

        self.auth_mode = auth_mode.lower()
        self.account_id = account_id or os.getenv('BASECAMP_ACCOUNT_ID')
        self.user_agent = user_agent or os.getenv('USER_AGENT')

        # Create a session for connection pooling
        self.session = requests.Session()

        # Set up authentication based on mode
        if self.auth_mode == 'basic':
            self.username = username or os.getenv('BASECAMP_USERNAME')
            self.password = password or os.getenv('BASECAMP_PASSWORD')

            if not all([self.username, self.password, self.account_id, self.user_agent]):
                raise ValueError("Missing required credentials for Basic Auth. Set them in .env file or pass them to the constructor.")

            self.auth = (self.username, self.password)
            self.session.auth = self.auth
            self.session.headers.update({
                "User-Agent": self.user_agent,
                "Content-Type": "application/json"
            })
            # Keep for backwards compat (used by create_attachment)
            self.headers = dict(self.session.headers)

        elif self.auth_mode == 'oauth':
            self.access_token = access_token or os.getenv('BASECAMP_ACCESS_TOKEN')

            if not all([self.access_token, self.account_id, self.user_agent]):
                raise ValueError("Missing required credentials for OAuth. Set them in .env file or pass them to the constructor.")

            self.auth = None  # No basic auth needed for OAuth
            self.session.headers.update({
                "User-Agent": self.user_agent,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}"
            })
            self.headers = dict(self.session.headers)

        else:
            raise ValueError("Invalid auth_mode. Must be 'basic' or 'oauth'")

        # Basecamp 3 uses a different URL structure
        self.base_url = f"https://3.basecampapi.com/{self.account_id}"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _check_response(self, response, context="API call", expected_statuses=(200,)):
        """Check HTTP response, parse JSON safely, or raise typed exception.

        Args:
            response: requests.Response object
            context: Human-readable description for error messages
            expected_statuses: Tuple of acceptable HTTP status codes

        Returns:
            Parsed JSON (dict/list) for 200/201, True for 204

        Raises:
            AuthenticationError, ForbiddenError, NotFoundError,
            RateLimitError, ServerError, BasecampError
        """
        status = response.status_code

        if status in expected_statuses:
            if status == 204:
                return True
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError) as e:
                raise BasecampError(
                    f"{context}: Invalid JSON in response body — {e}",
                    status_code=status
                )

        # Build a safe error snippet (avoid dumping huge HTML pages)
        try:
            error_body = response.json()
            error_detail = json.dumps(error_body)[:300]
        except Exception:
            error_detail = response.text[:300]

        if status == 401:
            raise AuthenticationError(
                f"{context}: 401 Unauthorized — token may be expired. {error_detail}",
                status_code=401
            )
        elif status == 403:
            raise ForbiddenError(
                f"{context}: 403 Forbidden — insufficient permissions. {error_detail}",
                status_code=403
            )
        elif status == 404:
            raise NotFoundError(
                f"{context}: 404 Not Found. {error_detail}",
                status_code=404
            )
        elif status == 429:
            retry_after = response.headers.get('Retry-After')
            raise RateLimitError(
                f"{context}: 429 Rate limited. Retry-After: {retry_after}. {error_detail}",
                retry_after=retry_after
            )
        elif status >= 500:
            raise ServerError(
                f"{context}: Server error {status}. {error_detail}",
                status_code=status
            )
        else:
            raise BasecampError(
                f"{context}: Unexpected HTTP {status}. {error_detail}",
                status_code=status
            )

    def test_connection(self):
        """Test the connection to Basecamp API."""
        response = self.get('projects.json')
        if response.status_code == 200:
            return True, "Connection successful"
        else:
            return False, f"Connection failed: {response.status_code} - {response.text[:200]}"

    def get(self, endpoint, params=None):
        """Make a GET request to the Basecamp API."""
        url = f"{self.base_url}/{endpoint}"
        return self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)

    def post(self, endpoint, data=None):
        """Make a POST request to the Basecamp API."""
        url = f"{self.base_url}/{endpoint}"
        return self.session.post(url, json=data, timeout=REQUEST_TIMEOUT)

    def put(self, endpoint, data=None):
        """Make a PUT request to the Basecamp API."""
        url = f"{self.base_url}/{endpoint}"
        return self.session.put(url, json=data, timeout=REQUEST_TIMEOUT)

    def delete(self, endpoint):
        """Make a DELETE request to the Basecamp API."""
        url = f"{self.base_url}/{endpoint}"
        return self.session.delete(url, timeout=REQUEST_TIMEOUT)

    def patch(self, endpoint, data=None):
        """Make a PATCH request to the Basecamp API."""
        url = f"{self.base_url}/{endpoint}"
        return self.session.patch(url, json=data, timeout=REQUEST_TIMEOUT)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _fetch_all_pages(self, endpoint, params=None):
        """Fetch all pages of a paginated list endpoint.

        Follows the Link rel="next" header until exhausted or MAX_PAGES.

        Returns:
            list: Aggregated items from all pages.
        """
        params = dict(params or {})
        all_items = []
        page = params.pop("page", 1)

        while page <= MAX_PAGES:
            params["page"] = page
            response = self.get(endpoint, params=params)
            items = self._check_response(response, context=f"GET {endpoint} page={page}")

            if not isinstance(items, list):
                # Some endpoints may return a dict; return as-is
                return items

            all_items.extend(items)

            link_header = response.headers.get("Link", "")
            if not items or 'rel="next"' not in link_header:
                break

            page += 1

        return all_items

    # ------------------------------------------------------------------
    # Project methods
    # ------------------------------------------------------------------

    def get_projects(self):
        """Get all projects."""
        return self._fetch_all_pages('projects.json')

    def get_project(self, project_id):
        """Get a specific project by ID."""
        response = self.get(f'projects/{project_id}.json')
        return self._check_response(response, context=f"Get project {project_id}")

    # ------------------------------------------------------------------
    # To-do list methods
    # ------------------------------------------------------------------

    def get_todoset(self, project_id):
        """Get the todoset for a project (Basecamp 3 has one todoset per project)."""
        project = self.get_project(project_id)
        dock = project.get("dock")
        if not isinstance(dock, list):
            raise BasecampError(f"No dock found in project {project_id}")
        todoset = next((item for item in dock if item.get("name") == "todoset"), None)
        if not todoset:
            raise NotFoundError(f"No todoset found in project {project_id}", status_code=404)
        return todoset

    def get_todolists(self, project_id):
        """Get all todolists for a project."""
        todoset = self.get_todoset(project_id)
        todoset_id = todoset['id']
        return self._fetch_all_pages(f'buckets/{project_id}/todosets/{todoset_id}/todolists.json')

    def get_todolist(self, todolist_id):
        """Get a specific todolist."""
        response = self.get(f'todolists/{todolist_id}.json')
        return self._check_response(response, context=f"Get todolist {todolist_id}")

    # ------------------------------------------------------------------
    # To-do methods
    # ------------------------------------------------------------------

    def get_todos(self, project_id, todolist_id, completed=None):
        """Get all todos in a todolist, handling pagination.

        Args:
            project_id: Project ID
            todolist_id: Todolist ID
            completed: If True, return only completed todos. If False, return only
                       incomplete todos. If None (default), return incomplete todos
                       (Basecamp API default behavior).
        """
        params = {}
        if completed is not None:
            params["completed"] = str(completed).lower()
        return self._fetch_all_pages(
            f'buckets/{project_id}/todolists/{todolist_id}/todos.json',
            params=params
        )

    def get_todo(self, todo_id):
        """Get a specific todo."""
        response = self.get(f'todos/{todo_id}.json')
        return self._check_response(response, context=f"Get todo {todo_id}")

    def create_todo(self, project_id, todolist_id, content, description=None, assignee_ids=None,
                    completion_subscriber_ids=None, notify=False, due_on=None, starts_on=None):
        """
        Create a new todo item in a todolist.

        Args:
            project_id (str): Project ID
            todolist_id (str): Todolist ID
            content (str): The todo item's text (required)
            description (str, optional): HTML description
            assignee_ids (list, optional): List of person IDs to assign
            completion_subscriber_ids (list, optional): List of person IDs to notify on completion
            notify (bool, optional): Whether to notify assignees
            due_on (str, optional): Due date in YYYY-MM-DD format
            starts_on (str, optional): Start date in YYYY-MM-DD format

        Returns:
            dict: The created todo
        """
        endpoint = f'buckets/{project_id}/todolists/{todolist_id}/todos.json'
        data = {'content': content}

        if description is not None:
            data['description'] = description
        if assignee_ids is not None:
            data['assignee_ids'] = assignee_ids
        if completion_subscriber_ids is not None:
            data['completion_subscriber_ids'] = completion_subscriber_ids
        if notify is not None:
            data['notify'] = notify
        if due_on is not None:
            data['due_on'] = due_on
        if starts_on is not None:
            data['starts_on'] = starts_on

        response = self.post(endpoint, data)
        return self._check_response(response, context="Create todo", expected_statuses=(201,))

    def update_todo(self, project_id, todo_id, content=None, description=None, assignee_ids=None,
                    completion_subscriber_ids=None, notify=None, due_on=None, starts_on=None):
        """
        Update an existing todo item.

        Args:
            project_id (str): Project ID
            todo_id (str): Todo ID
            content (str, optional): The todo item's text
            description (str, optional): HTML description
            assignee_ids (list, optional): List of person IDs to assign
            completion_subscriber_ids (list, optional): List of person IDs to notify on completion
            notify (bool, optional): Whether to notify assignees
            due_on (str, optional): Due date in YYYY-MM-DD format
            starts_on (str, optional): Start date in YYYY-MM-DD format

        Returns:
            dict: The updated todo
        """
        endpoint = f'buckets/{project_id}/todos/{todo_id}.json'
        data = {}

        if content is not None:
            data['content'] = content
        if description is not None:
            data['description'] = description
        if assignee_ids is not None:
            data['assignee_ids'] = assignee_ids
        if completion_subscriber_ids is not None:
            data['completion_subscriber_ids'] = completion_subscriber_ids
        if notify is not None:
            data['notify'] = notify
        if due_on is not None:
            data['due_on'] = due_on
        if starts_on is not None:
            data['starts_on'] = starts_on

        if not data:
            raise ValueError("No fields provided to update")

        response = self.put(endpoint, data)
        return self._check_response(response, context=f"Update todo {todo_id}")

    def delete_todo(self, project_id, todo_id):
        """
        Delete a todo item.

        Args:
            project_id (str): Project ID
            todo_id (str): Todo ID

        Returns:
            bool: True if successful
        """
        endpoint = f'buckets/{project_id}/todos/{todo_id}.json'
        response = self.delete(endpoint)
        return self._check_response(response, context=f"Delete todo {todo_id}", expected_statuses=(204,))

    def complete_todo(self, project_id, todo_id):
        """
        Mark a todo as complete.

        Args:
            project_id (str): Project ID
            todo_id (str): Todo ID

        Returns:
            dict: Completion details
        """
        endpoint = f'buckets/{project_id}/todos/{todo_id}/completion.json'
        response = self.post(endpoint)
        return self._check_response(response, context=f"Complete todo {todo_id}", expected_statuses=(201,))

    def uncomplete_todo(self, project_id, todo_id):
        """
        Mark a todo as incomplete.

        Args:
            project_id (str): Project ID
            todo_id (str): Todo ID

        Returns:
            bool: True if successful
        """
        endpoint = f'buckets/{project_id}/todos/{todo_id}/completion.json'
        response = self.delete(endpoint)
        return self._check_response(response, context=f"Uncomplete todo {todo_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # People methods
    # ------------------------------------------------------------------

    def get_people(self):
        """Get all people in the account."""
        return self._fetch_all_pages('people.json')

    # ------------------------------------------------------------------
    # Campfire (chat) methods
    # ------------------------------------------------------------------

    def get_campfires(self, project_id):
        """Get the campfire for a project."""
        response = self.get(f'buckets/{project_id}/chats.json')
        return self._check_response(response, context=f"Get campfires for project {project_id}")

    def get_campfire_lines(self, project_id, campfire_id):
        """Get chat lines from a campfire."""
        return self._fetch_all_pages(f'buckets/{project_id}/chats/{campfire_id}/lines.json')

    # ------------------------------------------------------------------
    # Message board methods
    # ------------------------------------------------------------------

    def get_message_board(self, project_id):
        """Get the message board for a project."""
        project = self.get_project(project_id)

        message_board_tool = None
        for tool in project.get('dock', []):
            if tool.get('name') == 'message_board':
                message_board_tool = tool
                break

        if not message_board_tool:
            raise NotFoundError(f"Message board not enabled for project {project_id}", status_code=404)

        message_board_id = message_board_tool.get('id')
        response = self.get(f'buckets/{project_id}/message_boards/{message_board_id}.json')
        return self._check_response(response, context=f"Get message board for project {project_id}")

    def get_messages(self, project_id):
        """Get all messages for a project."""
        message_board = self.get_message_board(project_id)
        message_board_id = message_board['id']
        return self._fetch_all_pages(f'buckets/{project_id}/message_boards/{message_board_id}/messages.json')

    def get_message(self, project_id, message_id):
        """Get a single message by ID."""
        response = self.get(f'buckets/{project_id}/messages/{message_id}.json')
        return self._check_response(response, context=f"Get message {message_id}")

    # ------------------------------------------------------------------
    # Schedule methods
    # ------------------------------------------------------------------

    def get_schedule(self, project_id):
        """Get the schedule for a project."""
        response = self.get(f'projects/{project_id}/schedule.json')
        return self._check_response(response, context=f"Get schedule for project {project_id}")

    def get_schedule_entries(self, project_id):
        """
        Get schedule entries for a project.

        Args:
            project_id (int): Project ID

        Returns:
            list: Schedule entries
        """
        endpoint = f"buckets/{project_id}/schedules.json"
        response = self.get(endpoint)
        schedule = self._check_response(response, context=f"Get schedules for project {project_id}")

        if isinstance(schedule, list) and len(schedule) > 0:
            schedule_id = schedule[0]['id']
            entries_endpoint = f"buckets/{project_id}/schedules/{schedule_id}/entries.json"
            return self._fetch_all_pages(entries_endpoint)
        else:
            return []

    # ------------------------------------------------------------------
    # Comments methods
    # ------------------------------------------------------------------

    def get_comments(self, project_id, recording_id):
        """
        Get all comments for a recording (todo, message, etc.).

        Args:
            recording_id (int): ID of the recording (todo, message, etc.)
            project_id (int): Project/bucket ID

        Returns:
            list: Comments for the recording
        """
        return self._fetch_all_pages(f"buckets/{project_id}/recordings/{recording_id}/comments.json")

    def create_comment(self, recording_id, bucket_id, content):
        """
        Create a comment on a recording.

        Args:
            recording_id (int): ID of the recording to comment on
            bucket_id (int): Project/bucket ID
            content (str): Content of the comment in HTML format

        Returns:
            dict: The created comment
        """
        endpoint = f"buckets/{bucket_id}/recordings/{recording_id}/comments.json"
        data = {"content": content}
        response = self.post(endpoint, data)
        return self._check_response(response, context="Create comment", expected_statuses=(201,))

    def get_comment(self, comment_id, bucket_id):
        """
        Get a specific comment.

        Args:
            comment_id (int): Comment ID
            bucket_id (int): Project/bucket ID

        Returns:
            dict: Comment details
        """
        endpoint = f"buckets/{bucket_id}/comments/{comment_id}.json"
        response = self.get(endpoint)
        return self._check_response(response, context=f"Get comment {comment_id}")

    def update_comment(self, comment_id, bucket_id, content):
        """
        Update a comment.

        Args:
            comment_id (int): Comment ID
            bucket_id (int): Project/bucket ID
            content (str): New content for the comment in HTML format

        Returns:
            dict: Updated comment
        """
        endpoint = f"buckets/{bucket_id}/comments/{comment_id}.json"
        data = {"content": content}
        response = self.put(endpoint, data)
        return self._check_response(response, context=f"Update comment {comment_id}")

    def delete_comment(self, comment_id, bucket_id):
        """
        Delete a comment.

        Args:
            comment_id (int): Comment ID
            bucket_id (int): Project/bucket ID

        Returns:
            bool: True if successful
        """
        endpoint = f"buckets/{bucket_id}/comments/{comment_id}.json"
        response = self.delete(endpoint)
        return self._check_response(response, context=f"Delete comment {comment_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Daily check-in methods
    # ------------------------------------------------------------------

    def get_daily_check_ins(self, project_id, page=1):
        project = self.get_project(project_id)
        dock = project.get("dock", [])
        questionnaire = next((item for item in dock if item.get("name") == "questionnaire"), None)
        if not questionnaire:
            raise NotFoundError(f"No questionnaire found in project {project_id}", status_code=404)
        endpoint = f"buckets/{project_id}/questionnaires/{questionnaire['id']}/questions.json"
        response = self.get(endpoint, params={"page": page})
        return self._check_response(response, context="Get daily check-ins")

    def get_question_answers(self, project_id, question_id, page=1):
        endpoint = f"buckets/{project_id}/questions/{question_id}/answers.json"
        response = self.get(endpoint, params={"page": page})
        return self._check_response(response, context=f"Get answers for question {question_id}")

    # ------------------------------------------------------------------
    # Card Table methods
    # ------------------------------------------------------------------

    def get_card_tables(self, project_id):
        """Get all card tables for a project."""
        project = self.get_project(project_id)
        dock = project.get("dock", [])
        return [item for item in dock if item.get("name") in ("kanban_board", "card_table")]

    def get_card_table(self, project_id):
        """Get the first card table for a project (Basecamp 3 can have multiple card tables per project)."""
        card_tables = self.get_card_tables(project_id)
        if not card_tables:
            raise NotFoundError(f"No card tables found for project {project_id}", status_code=404)
        return card_tables[0]

    def get_card_table_details(self, project_id, card_table_id):
        """Get details for a specific card table."""
        response = self.get(f'buckets/{project_id}/card_tables/{card_table_id}.json')
        return self._check_response(
            response, context=f"Get card table {card_table_id}",
            expected_statuses=(200, 204)
        )

    # ------------------------------------------------------------------
    # Card Table Column methods
    # ------------------------------------------------------------------

    def get_columns(self, project_id, card_table_id):
        """Get all columns in a card table."""
        card_table_details = self.get_card_table_details(project_id, card_table_id)
        if card_table_details is True:
            # 204 No Content
            return []
        return card_table_details.get('lists', [])

    def get_column(self, project_id, column_id):
        """Get a specific column."""
        response = self.get(f'buckets/{project_id}/card_tables/columns/{column_id}.json')
        return self._check_response(response, context=f"Get column {column_id}")

    def create_column(self, project_id, card_table_id, title):
        """Create a new column in a card table."""
        data = {"title": title}
        response = self.post(f'buckets/{project_id}/card_tables/{card_table_id}/columns.json', data)
        return self._check_response(response, context="Create column", expected_statuses=(201,))

    def update_column(self, project_id, column_id, title):
        """Update a column title."""
        data = {"title": title}
        response = self.put(f'buckets/{project_id}/card_tables/columns/{column_id}.json', data)
        return self._check_response(response, context=f"Update column {column_id}")

    def move_column(self, project_id, column_id, position, card_table_id):
        """Move a column to a new position."""
        data = {
            "source_id": column_id,
            "target_id": card_table_id,
            "position": position
        }
        response = self.post(f'buckets/{project_id}/card_tables/{card_table_id}/moves.json', data)
        return self._check_response(response, context="Move column", expected_statuses=(204,))

    def update_column_color(self, project_id, column_id, color):
        """Update a column color."""
        data = {"color": color}
        response = self.patch(f'buckets/{project_id}/card_tables/columns/{column_id}/color.json', data)
        return self._check_response(response, context=f"Update column {column_id} color")

    def put_column_on_hold(self, project_id, column_id):
        """Put a column on hold."""
        response = self.post(f'buckets/{project_id}/card_tables/columns/{column_id}/on_hold.json')
        return self._check_response(response, context="Put column on hold", expected_statuses=(204,))

    def remove_column_hold(self, project_id, column_id):
        """Remove hold from a column."""
        response = self.delete(f'buckets/{project_id}/card_tables/columns/{column_id}/on_hold.json')
        return self._check_response(response, context="Remove column hold", expected_statuses=(204,))

    def watch_column(self, project_id, column_id):
        """Subscribe to column notifications."""
        response = self.post(f'buckets/{project_id}/card_tables/lists/{column_id}/subscription.json')
        return self._check_response(response, context="Watch column", expected_statuses=(204,))

    def unwatch_column(self, project_id, column_id):
        """Unsubscribe from column notifications."""
        response = self.delete(f'buckets/{project_id}/card_tables/lists/{column_id}/subscription.json')
        return self._check_response(response, context="Unwatch column", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Card Table Card methods
    # ------------------------------------------------------------------

    def get_cards(self, project_id, column_id):
        """Get all cards in a column."""
        return self._fetch_all_pages(f'buckets/{project_id}/card_tables/lists/{column_id}/cards.json')

    def get_card(self, project_id, card_id):
        """Get a specific card."""
        response = self.get(f'buckets/{project_id}/card_tables/cards/{card_id}.json')
        return self._check_response(response, context=f"Get card {card_id}")

    def create_card(self, project_id, column_id, title, content=None, due_on=None, notify=False):
        """Create a new card in a column."""
        data = {"title": title}
        if content:
            data["content"] = content
        if due_on:
            data["due_on"] = due_on
        if notify:
            data["notify"] = notify
        response = self.post(f'buckets/{project_id}/card_tables/lists/{column_id}/cards.json', data)
        return self._check_response(response, context="Create card", expected_statuses=(201,))

    def update_card(self, project_id, card_id, title=None, content=None, due_on=None, assignee_ids=None):
        """Update a card."""
        data = {}
        if title:
            data["title"] = title
        if content:
            data["content"] = content
        if due_on:
            data["due_on"] = due_on
        if assignee_ids:
            data["assignee_ids"] = assignee_ids
        response = self.put(f'buckets/{project_id}/card_tables/cards/{card_id}.json', data)
        return self._check_response(response, context=f"Update card {card_id}")

    def move_card(self, project_id, card_id, column_id):
        """Move a card to a new column."""
        data = {"column_id": column_id}
        response = self.post(f'buckets/{project_id}/card_tables/cards/{card_id}/moves.json', data)
        return self._check_response(response, context="Move card", expected_statuses=(204,))

    def complete_card(self, project_id, card_id):
        """Mark a card as complete."""
        response = self.post(f'buckets/{project_id}/todos/{card_id}/completion.json')
        return self._check_response(response, context=f"Complete card {card_id}", expected_statuses=(201,))

    def uncomplete_card(self, project_id, card_id):
        """Mark a card as incomplete."""
        response = self.delete(f'buckets/{project_id}/todos/{card_id}/completion.json')
        return self._check_response(response, context=f"Uncomplete card {card_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Card Steps methods
    # ------------------------------------------------------------------

    def get_card_steps(self, project_id, card_id):
        """Get all steps (sub-tasks) for a card."""
        card = self.get_card(project_id, card_id)
        return card.get('steps', [])

    def create_card_step(self, project_id, card_id, title, due_on=None, assignee_ids=None):
        """Create a new step (sub-task) for a card."""
        data = {"title": title}
        if due_on:
            data["due_on"] = due_on
        if assignee_ids:
            data["assignee_ids"] = assignee_ids
        response = self.post(f'buckets/{project_id}/card_tables/cards/{card_id}/steps.json', data)
        return self._check_response(response, context="Create card step", expected_statuses=(201,))

    def get_card_step(self, project_id, step_id):
        """Get a specific card step."""
        response = self.get(f'buckets/{project_id}/card_tables/steps/{step_id}.json')
        return self._check_response(response, context=f"Get card step {step_id}")

    def update_card_step(self, project_id, step_id, title=None, due_on=None, assignee_ids=None):
        """Update a card step."""
        data = {}
        if title:
            data["title"] = title
        if due_on:
            data["due_on"] = due_on
        if assignee_ids:
            data["assignee_ids"] = assignee_ids
        response = self.put(f'buckets/{project_id}/card_tables/steps/{step_id}.json', data)
        return self._check_response(response, context=f"Update card step {step_id}")

    def delete_card_step(self, project_id, step_id):
        """Delete a card step."""
        response = self.delete(f'buckets/{project_id}/card_tables/steps/{step_id}.json')
        return self._check_response(response, context=f"Delete card step {step_id}", expected_statuses=(204,))

    def complete_card_step(self, project_id, step_id):
        """Mark a card step as complete."""
        response = self.post(f'buckets/{project_id}/todos/{step_id}/completion.json')
        return self._check_response(response, context=f"Complete card step {step_id}", expected_statuses=(201,))

    def uncomplete_card_step(self, project_id, step_id):
        """Mark a card step as incomplete."""
        response = self.delete(f'buckets/{project_id}/todos/{step_id}/completion.json')
        return self._check_response(response, context=f"Uncomplete card step {step_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Attachment methods
    # ------------------------------------------------------------------

    def create_attachment(self, file_path, name, content_type="application/octet-stream"):
        """Upload an attachment and return the attachable sgid."""
        with open(file_path, "rb") as f:
            data = f.read()

        headers = dict(self.session.headers)
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(data))

        endpoint = f"attachments.json?name={name}"
        # Use raw requests.post here since we need custom headers and binary data
        response = requests.post(
            f"{self.base_url}/{endpoint}",
            auth=self.session.auth,
            headers=headers,
            data=data,
            timeout=REQUEST_TIMEOUT
        )
        return self._check_response(response, context="Create attachment", expected_statuses=(201,))

    # ------------------------------------------------------------------
    # Events & Webhooks
    # ------------------------------------------------------------------

    def get_events(self, project_id, recording_id):
        """Get events for a recording."""
        return self._fetch_all_pages(f"buckets/{project_id}/recordings/{recording_id}/events.json")

    def get_webhooks(self, project_id):
        """List webhooks for a project."""
        return self._fetch_all_pages(f"buckets/{project_id}/webhooks.json")

    def create_webhook(self, project_id, payload_url, types=None):
        """Create a webhook for a project."""
        data = {"payload_url": payload_url}
        if types:
            data["types"] = types
        endpoint = f"buckets/{project_id}/webhooks.json"
        response = self.post(endpoint, data)
        return self._check_response(response, context="Create webhook", expected_statuses=(201,))

    def delete_webhook(self, project_id, webhook_id):
        """Delete a webhook."""
        endpoint = f"buckets/{project_id}/webhooks/{webhook_id}.json"
        response = self.delete(endpoint)
        return self._check_response(response, context=f"Delete webhook {webhook_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Vault methods
    # ------------------------------------------------------------------

    def get_vaults(self, project_id):
        """Get the root vault (Docs & Files) for a project, including child vaults.

        Returns the vault object which contains nested vaults. Use the vault ID
        with get_documents() and get_uploads().
        """
        project = self.get_project(project_id)
        dock = project.get("dock", [])
        vault_tool = next((item for item in dock if item.get("name") == "vault"), None)
        if not vault_tool:
            raise NotFoundError(f"No vault (Docs & Files) found in project {project_id}", status_code=404)
        vault_id = vault_tool.get("id")
        response = self.get(f'buckets/{project_id}/vaults/{vault_id}.json')
        return self._check_response(response, context=f"Get vault for project {project_id}")

    # ------------------------------------------------------------------
    # Document methods
    # ------------------------------------------------------------------

    def get_documents(self, project_id, vault_id):
        """List documents in a vault."""
        return self._fetch_all_pages(f"buckets/{project_id}/vaults/{vault_id}/documents.json")

    def get_document(self, project_id, document_id):
        """Get a single document."""
        endpoint = f"buckets/{project_id}/documents/{document_id}.json"
        response = self.get(endpoint)
        return self._check_response(response, context=f"Get document {document_id}")

    def create_document(self, project_id, vault_id, title, content, status="active"):
        """Create a document in a vault."""
        data = {"title": title, "content": content, "status": status}
        endpoint = f"buckets/{project_id}/vaults/{vault_id}/documents.json"
        response = self.post(endpoint, data)
        return self._check_response(response, context="Create document", expected_statuses=(201,))

    def update_document(self, project_id, document_id, title=None, content=None):
        """Update a document's title or content."""
        data = {}
        if title:
            data["title"] = title
        if content:
            data["content"] = content
        endpoint = f"buckets/{project_id}/documents/{document_id}.json"
        response = self.put(endpoint, data)
        return self._check_response(response, context=f"Update document {document_id}")

    def trash_document(self, project_id, document_id):
        """Trash a document."""
        endpoint = f"buckets/{project_id}/recordings/{document_id}/status/trashed.json"
        response = self.put(endpoint)
        return self._check_response(response, context=f"Trash document {document_id}", expected_statuses=(204,))

    # ------------------------------------------------------------------
    # Upload methods
    # ------------------------------------------------------------------

    def get_uploads(self, project_id, vault_id=None):
        """List uploads in a project or vault."""
        if vault_id:
            endpoint = f"buckets/{project_id}/vaults/{vault_id}/uploads.json"
        else:
            endpoint = f"buckets/{project_id}/uploads.json"
        return self._fetch_all_pages(endpoint)

    def get_upload(self, project_id, upload_id):
        """Get a single upload."""
        endpoint = f"buckets/{project_id}/uploads/{upload_id}.json"
        response = self.get(endpoint)
        return self._check_response(response, context=f"Get upload {upload_id}")

    # ------------------------------------------------------------------
    # Search methods
    # ------------------------------------------------------------------

    def search(self, query, page=1):
        """
        Search across all Basecamp content using the native search API.

        Args:
            query (str): Search query string
            page (int, optional): Page number for pagination (default: 1)

        Returns:
            dict: Search results with 'results' list and optional 'next_page' indicator
        """
        endpoint = "search.json"
        params = {"query": query, "page": page}
        response = self.get(endpoint, params=params)
        results = self._check_response(response, context=f"Search '{query}'")

        result_dict = {"results": results}
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            result_dict['has_next_page'] = True
            result_dict['next_page'] = page + 1
        else:
            result_dict['has_next_page'] = False

        return result_dict
