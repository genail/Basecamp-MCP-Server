"""
Token storage module for securely storing OAuth tokens.

This module provides a simple interface for storing and retrieving OAuth tokens.
In a production environment, this should be replaced with a more secure solution
like a database or a secure token storage service.
"""

import os
import json
import threading
from datetime import datetime, timedelta
import logging

# Determine the directory where this script (token_storage.py) is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Define TOKEN_FILE as an absolute path within that directory
TOKEN_FILE = os.path.join(SCRIPT_DIR, 'oauth_tokens.json')

# Lock for thread-safe operations
_lock = threading.Lock()
_logger = logging.getLogger(__name__)

def _read_tokens():
    """Read tokens from storage."""
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
            basecamp_data = data.get('basecamp', {})
            updated_at = basecamp_data.get('updated_at')
            _logger.info(f"Read tokens from {TOKEN_FILE}. Basecamp token updated_at: {updated_at}")
            return data
    except FileNotFoundError:
        _logger.info(f"{TOKEN_FILE} not found. Returning empty tokens.")
        return {}  # Return empty dict if file doesn't exist
    except json.JSONDecodeError:
        _logger.warning(f"Error decoding JSON from {TOKEN_FILE}. Returning empty tokens.")
        # If file exists but isn't valid JSON, return empty dict
        return {}

def _write_tokens(tokens):
    """Write tokens to storage."""
    # Create directory for the token file if it doesn't exist
    os.makedirs(os.path.dirname(TOKEN_FILE) if os.path.dirname(TOKEN_FILE) else '.', exist_ok=True)

    basecamp_data_to_write = tokens.get('basecamp', {})
    updated_at_to_write = basecamp_data_to_write.get('updated_at')
    _logger.info(f"Writing tokens to {TOKEN_FILE}. Basecamp token updated_at to be written: {updated_at_to_write}")

    # Set secure permissions on the file
    with open(TOKEN_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)

    # Set permissions to only allow the current user to read/write
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass  # Ignore if chmod fails (might be on Windows)

def store_token(access_token, refresh_token=None, expires_in=None, account_id=None):
    """
    Store OAuth tokens securely.

    Args:
        access_token (str): The OAuth access token
        refresh_token (str, optional): The OAuth refresh token
        expires_in (int, optional): Token expiration time in seconds
        account_id (str, optional): The Basecamp account ID

    Returns:
        bool: True if the token was stored successfully
    """
    if not access_token:
        return False  # Don't store empty tokens

    with _lock:
        tokens = _read_tokens()

        # Calculate expiration time
        expires_at = None
        if expires_in:
            expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

        # Store the token with metadata
        tokens['basecamp'] = {
            'access_token': access_token,
            'refresh_token': refresh_token,
            'account_id': account_id,
            'expires_at': expires_at,
            'updated_at': datetime.now().isoformat()
        }

        _write_tokens(tokens)
        return True

def get_token():
    """
    Get the stored OAuth token.

    Returns:
        dict: Token information or None if not found
    """
    with _lock:
        tokens = _read_tokens()
        return tokens.get('basecamp')

def is_token_expired():
    """
    Check if the stored token is expired.

    Returns:
        bool: True if the token is expired or not found
    """
    with _lock:
        tokens = _read_tokens()
        token_data = tokens.get('basecamp')

        if not token_data or not token_data.get('expires_at'):
            return True

        try:
            expires_at = datetime.fromisoformat(token_data['expires_at'])
            # Add a buffer of 5 minutes to account for clock differences
            return datetime.now() > (expires_at - timedelta(minutes=5))
        except (ValueError, TypeError):
            return True

def clear_tokens():
    """Clear all stored tokens."""
    with _lock:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        return True


def _is_token_data_expired(token_data):
    """
    Check if the given token data is expired.
    Internal helper that doesn't acquire lock - caller must ensure thread safety.

    Args:
        token_data: Token dictionary with 'expires_at' field

    Returns:
        bool: True if the token is expired or invalid
    """
    if not token_data or not token_data.get('expires_at'):
        return True

    try:
        expires_at = datetime.fromisoformat(token_data['expires_at'])
        # Add a buffer of 5 minutes to account for clock differences
        return datetime.now() > (expires_at - timedelta(minutes=5))
    except (ValueError, TypeError):
        return True


def ensure_valid_token():
    """
    Ensure we have a valid, non-expired token.
    Attempts to refresh if expired.

    Thread-safe: Uses double-checked locking to prevent concurrent refresh attempts.

    Returns:
        dict: Valid token data or None if authentication is needed
    """
    # Quick check without lock (optimistic path for valid tokens)
    token_data = get_token()

    if not token_data or not token_data.get('access_token'):
        _logger.info("No token found")
        return None

    # Fast path: token is valid, no refresh needed
    if not _is_token_data_expired(token_data):
        _logger.info("Token is valid")
        return token_data

    # Token appears expired - acquire lock for refresh
    with _lock:
        # Double-check: another thread may have refreshed while we waited for lock
        token_data = _read_tokens().get('basecamp')

        if not token_data or not token_data.get('access_token'):
            _logger.info("No token found (after lock)")
            return None

        # Re-check expiration under lock
        if not _is_token_data_expired(token_data):
            _logger.info("Token was refreshed by another thread")
            return token_data

        _logger.info("Token is expired, attempting to refresh")

        refresh_token_value = token_data.get('refresh_token')
        if not refresh_token_value:
            _logger.warning("No refresh token available, user needs to re-authenticate")
            return None

        try:
            # Import here to avoid circular imports
            from basecamp_oauth import BasecampOAuth

            oauth_client = BasecampOAuth()
            new_token_data = oauth_client.refresh_token(refresh_token_value)

            # Store the new token
            access_token = new_token_data.get('access_token')
            # Use old refresh token if new one not provided
            new_refresh_token = new_token_data.get('refresh_token', refresh_token_value)
            expires_in = new_token_data.get('expires_in')
            account_id = token_data.get('account_id')  # Keep the existing account_id

            if access_token:
                # Calculate expiration time
                expires_at = None
                if expires_in:
                    expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

                # Write directly since we already hold the lock
                tokens = _read_tokens()
                tokens['basecamp'] = {
                    'access_token': access_token,
                    'refresh_token': new_refresh_token,
                    'account_id': account_id,
                    'expires_at': expires_at,
                    'updated_at': datetime.now().isoformat()
                }
                _write_tokens(tokens)

                _logger.info("Token refreshed successfully")
                return tokens['basecamp']
            else:
                _logger.error("No access token in refresh response")
                return None

        except Exception as e:
            _logger.error("Failed to refresh token: %s", str(e))
            return None
