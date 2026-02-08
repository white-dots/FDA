"""
API data adapter.

Pulls data from REST APIs using requests library.
"""

import logging
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from fda.data.base import DataAdapter

logger = logging.getLogger(__name__)


class APIAdapter(DataAdapter):
    """
    Adapter for pulling data from REST APIs.

    Supports configurable endpoints and multiple authentication methods.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the API adapter.

        Args:
            config: Configuration dictionary containing:
                   - base_url: Base URL for the API
                   - endpoints: Dict of metric names to endpoint configs
                   - auth: Optional auth config (type, credentials)
                   - headers: Optional custom headers
                   - timeout: Request timeout in seconds (default 30)
                   - verify_ssl: Whether to verify SSL certificates (default True)
        """
        self.config = config
        self.base_url = config.get("base_url", "").rstrip("/")
        self.endpoints = config.get("endpoints", {})
        self.auth_config = config.get("auth")
        self.headers = config.get("headers", {})
        self.timeout = config.get("timeout", 30)
        self.verify_ssl = config.get("verify_ssl", True)

        # Set up session for connection pooling
        self.session = requests.Session()
        self._setup_auth()
        self._setup_headers()

    def _setup_auth(self) -> None:
        """Configure authentication for the session."""
        if not self.auth_config:
            return

        auth_type = self.auth_config.get("type", "").lower()

        if auth_type == "basic":
            username = self.auth_config.get("username", "")
            password = self.auth_config.get("password", "")
            self.session.auth = HTTPBasicAuth(username, password)

        elif auth_type == "bearer":
            token = self.auth_config.get("token", "")
            self.session.headers["Authorization"] = f"Bearer {token}"

        elif auth_type == "api_key":
            key_name = self.auth_config.get("key_name", "X-API-Key")
            key_value = self.auth_config.get("key_value", "")
            location = self.auth_config.get("location", "header")

            if location == "header":
                self.session.headers[key_name] = key_value
            # Query param auth is handled per-request

        elif auth_type == "oauth2":
            # OAuth2 token should be pre-obtained and provided
            token = self.auth_config.get("access_token", "")
            token_type = self.auth_config.get("token_type", "Bearer")
            self.session.headers["Authorization"] = f"{token_type} {token}"

    def _setup_headers(self) -> None:
        """Configure custom headers for the session."""
        for key, value in self.headers.items():
            self.session.headers[key] = value

        # Set default content type if not specified
        if "Content-Type" not in self.session.headers:
            self.session.headers["Content-Type"] = "application/json"
        if "Accept" not in self.session.headers:
            self.session.headers["Accept"] = "application/json"

    def test_connection(self) -> bool:
        """
        Test connection to the API.

        Returns:
            True if API is reachable, False otherwise.
        """
        # Try to access the base URL or a health endpoint
        test_endpoint = self.config.get("health_endpoint", "")

        try:
            if test_endpoint:
                url = urljoin(self.base_url + "/", test_endpoint)
            else:
                url = self.base_url

            response = self.session.get(
                url,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            return response.status_code < 500

        except requests.RequestException as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def pull_latest(self, metric: Optional[str] = None) -> dict[str, Any]:
        """
        Pull latest data from the API.

        Args:
            metric: Specific metric to pull, or None for all.

        Returns:
            Dictionary of metric names to values.
        """
        results: dict[str, Any] = {}

        endpoints_to_query = (
            {metric: self.endpoints[metric]}
            if metric and metric in self.endpoints
            else self.endpoints
        )

        for metric_name, endpoint_config in endpoints_to_query.items():
            try:
                data = self._fetch_metric(metric_name, endpoint_config)
                if data is not None:
                    results[metric_name] = data
            except Exception as e:
                logger.error(f"Failed to pull metric '{metric_name}': {e}")
                results[metric_name] = {"error": str(e)}

        return results

    def _fetch_metric(
        self, metric_name: str, endpoint_config: Any
    ) -> Optional[Any]:
        """
        Fetch a single metric from its endpoint.

        Args:
            metric_name: Name of the metric.
            endpoint_config: Endpoint configuration (string path or dict).

        Returns:
            The fetched data or None if error.
        """
        # Handle simple string endpoint or complex config
        if isinstance(endpoint_config, str):
            endpoint = endpoint_config
            method = "GET"
            params = {}
            json_path = None
        else:
            endpoint = endpoint_config.get("path", "")
            method = endpoint_config.get("method", "GET").upper()
            params = endpoint_config.get("params", {})
            json_path = endpoint_config.get("json_path")

        response = self._make_request(method, endpoint, params=params)

        if response is None:
            return None

        # Extract value using json_path if specified
        if json_path:
            return self._extract_json_path(response, json_path)

        return response

    def _extract_json_path(self, data: Any, path: str) -> Any:
        """
        Extract a value from nested JSON using dot notation.

        Args:
            data: The JSON data.
            path: Dot-separated path (e.g., "data.metrics.value").

        Returns:
            The extracted value.
        """
        parts = path.split(".")
        current = data

        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                return None

            if current is None:
                return None

        return current

    def get_schema(self) -> dict[str, Any]:
        """
        Get the schema of available metrics.

        Returns:
            Dictionary describing available metrics.
        """
        schema: dict[str, Any] = {
            "base_url": self.base_url,
            "metrics": {},
        }

        for metric_name, endpoint_config in self.endpoints.items():
            if isinstance(endpoint_config, str):
                schema["metrics"][metric_name] = {
                    "path": endpoint_config,
                    "method": "GET",
                }
            else:
                schema["metrics"][metric_name] = {
                    "path": endpoint_config.get("path", ""),
                    "method": endpoint_config.get("method", "GET"),
                    "params": endpoint_config.get("params", {}),
                    "json_path": endpoint_config.get("json_path"),
                }

        return schema

    def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Any:
        """
        Make an authenticated request to the API.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: Endpoint path.
            **kwargs: Additional arguments for requests.

        Returns:
            Response JSON or None if error.
        """
        url = urljoin(self.base_url + "/", endpoint.lstrip("/"))

        # Handle API key in query params if configured
        if self.auth_config and self.auth_config.get("type") == "api_key":
            if self.auth_config.get("location") == "query":
                params = kwargs.get("params", {})
                key_name = self.auth_config.get("key_name", "api_key")
                key_value = self.auth_config.get("key_value", "")
                params[key_name] = key_value
                kwargs["params"] = params

        try:
            response = self.session.request(
                method=method,
                url=url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                **kwargs,
            )
            response.raise_for_status()

            # Try to parse as JSON
            try:
                return response.json()
            except ValueError:
                return response.text

        except requests.HTTPError as e:
            logger.error(f"HTTP error for {url}: {e}")
            raise
        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    def post(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Any:
        """
        Make a POST request to the API.

        Args:
            endpoint: Endpoint path.
            data: Form data to send.
            json_data: JSON data to send.

        Returns:
            Response JSON or None if error.
        """
        kwargs: dict[str, Any] = {}
        if data:
            kwargs["data"] = data
        if json_data:
            kwargs["json"] = json_data

        return self._make_request("POST", endpoint, **kwargs)

    def close(self) -> None:
        """Close the session and release resources."""
        self.session.close()
