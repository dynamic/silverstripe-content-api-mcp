"""Vendored from dynamic/daisy-base at commit 612a4155d7696c692e82a3376ce94e119a60b141
(mcp_base/http.py). See content_api_mcp/_base/__init__.py for why.

Only create_http_session is used by this repo's client.py; AuthenticatedSession
and BaseAPIClient are kept for fidelity with the source file (issue #27 asked
for the real implementations, not a reconstruction from call sites) but have
no callers here.

Bump this file deliberately when daisy-base changes create_http_session's
retry/header defaults — it is not kept in sync automatically.
"""

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)

__all__ = [
    "create_http_session",
    "AuthenticatedSession",
    "BaseAPIClient",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
]

DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = "MCP-Server/1.0"


def create_http_session(
    user_agent: str | None = None,
    max_retries: int = 3,
    backoff_factor: float = 0.3,
    status_forcelist: tuple | None = None,
) -> requests.Session:
    """
    Create a consistently configured HTTP session.

    All MCP servers should use this for external API calls to ensure
    consistent behavior for retries and error handling.

    Note: Timeout must be passed to each request call (requests library
    limitation). Use AuthenticatedSession for automatic timeout handling.

    Args:
        user_agent: Custom User-Agent header (default: MCP-Server/1.0)
        max_retries: Number of retry attempts for failed requests (default: 3)
        backoff_factor: Exponential backoff factor between retries (default: 0.3)
        status_forcelist: HTTP status codes to retry (default: 429, 500, 502, 503, 504)

    Returns:
        Configured requests.Session with retry logic and default headers

    Example:
        session = create_http_session(user_agent="Teamwork-MCP/1.0")

        # Timeout must be passed per-request
        response = session.get(
            "https://api.teamwork.com/projects",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    """
    if status_forcelist is None:
        status_forcelist = (429, 500, 502, 503, 504)

    session = requests.Session()

    # Configure retry strategy (idempotent methods only to prevent duplicate operations)
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "OPTIONS"],  # Only idempotent methods
        raise_on_status=False,  # Don't raise, let caller handle
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Set safe default headers; callers should set Content-Type per request when sending a body
    session.headers.update({
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "application/json",
    })

    LOGGER.debug(
        "Created HTTP session: retries=%d, user_agent=%s",
        max_retries,
        user_agent or DEFAULT_USER_AGENT,
    )

    return session


class AuthenticatedSession:
    """
    HTTP session wrapper that automatically includes authentication headers.

    Useful for API clients that need to make many authenticated requests.

    Example:
        session = AuthenticatedSession(
            access_token="...",
            base_url="https://api.teamwork.com/v3",
            timeout=30,
        )

        # Token is automatically included
        data = session.get("/projects").json()
    """

    def __init__(
        self,
        access_token: str,
        base_url: str,
        timeout: int = DEFAULT_TIMEOUT,
        user_agent: str | None = None,
        token_type: str = "Bearer",
    ):
        """
        Initialize authenticated session.

        Args:
            access_token: OAuth access token
            base_url: Base URL for all requests (e.g., "https://api.example.com/v1")
            timeout: Request timeout in seconds
            user_agent: Custom User-Agent header
            token_type: Token type for Authorization header (default: Bearer)
        """
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token_type = token_type
        self._session = create_http_session(user_agent=user_agent)
        self._session.headers["Authorization"] = f"{token_type} {access_token}"

    def request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response:
        """
        Make an authenticated request.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            path: URL path (will be appended to base_url)
            **kwargs: Additional arguments passed to requests

        Returns:
            requests.Response object
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        return self._session.request(method, url, **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        """Make GET request."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        """Make POST request."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        """Make PUT request."""
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        """Make PATCH request."""
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        """Make DELETE request."""
        return self.request("DELETE", path, **kwargs)


class BaseAPIClient:
    """
    Base class for OAuth API clients.

    Provides common HTTP request handling with:
    - Bearer token authentication
    - Consistent timeout handling
    - Structured error logging and raising

    Subclasses should set BASE_URL and implement domain-specific methods.

    Example:
        class HubSpotClient(BaseAPIClient):
            BASE_URL = "https://api.hubapi.com"

            def list_contacts(self, limit: int = 50):
                return self._request("GET", "/crm/v3/objects/contacts", params={"limit": limit})
    """

    BASE_URL: str = ""
    DEFAULT_TIMEOUT: int = 30

    def __init__(self, access_token: str, base_url: str = None):
        """
        Initialize API client.

        Args:
            access_token: OAuth 2.0 access token
            base_url: Optional override for BASE_URL
        """
        self.access_token = access_token
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self._logger = logging.getLogger(self.__class__.__name__)

    def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        json_data: dict = None,
        timeout: int = None,
    ) -> dict:
        """
        Make authenticated request to API.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            path: URL path (will be appended to base_url)
            params: Query parameters
            json_data: JSON body data
            timeout: Request timeout (defaults to DEFAULT_TIMEOUT)

        Returns:
            Response JSON as dict

        Raises:
            RuntimeError: If request fails
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=timeout or self.DEFAULT_TIMEOUT,
            )
            response.raise_for_status()

            # Handle empty responses (204 No Content)
            if response.status_code == 204 or not response.content:
                return {"success": True}

            return response.json()

        except requests.exceptions.HTTPError as e:
            self._logger.error(
                "%s API error %d: %s",
                self.__class__.__name__,
                e.response.status_code,
                e.response.text,
            )
            raise RuntimeError(
                f"{self.__class__.__name__} API error {e.response.status_code}: {e.response.text}"
            )
        except requests.exceptions.RequestException as e:
            self._logger.error("%s request failed: %s", self.__class__.__name__, e)
            raise RuntimeError(f"{self.__class__.__name__} request failed: {e}")
