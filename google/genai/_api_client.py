# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""Base client for calling HTTP APIs sending and receiving JSON.

The BaseApiClient is intended to be a private module and is subject to change.
"""

import asyncio
from collections.abc import Awaitable, Generator
import copy
from dataclasses import dataclass
import datetime
import http
import inspect
import io
import json
import logging
import math
import os
import ssl
import sys
import threading
import time
from typing import Any, AsyncIterator, Optional, TYPE_CHECKING, Tuple, Union
from urllib.parse import urlparse
from urllib.parse import urlunparse

import anyio
import certifi
import google.auth
import google.auth.credentials
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
import httpx
from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError
import tenacity

from . import _common
from . import errors
from . import version
from .types import HttpOptions
from .types import HttpOptionsDict
from .types import HttpOptionsOrDict
from .types import HttpResponse as SdkHttpResponse
from .types import HttpRetryOptions


has_aiohttp = False
try:
  import aiohttp

  has_aiohttp = True
except ImportError:
  pass

# internal comment


if TYPE_CHECKING:
  from multidict import CIMultiDictProxy


logger = logging.getLogger('google_genai._api_client')
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB chunk size
MAX_RETRY_COUNT = 3
INITIAL_RETRY_DELAY = 1  # second
DELAY_MULTIPLIER = 2


class EphemeralTokenAPIKeyError(ValueError):
  """Error raised when the API key is invalid."""


# This method checks for the API key in the environment variables. Google API
# key is precedenced over Gemini API key.
def _get_env_api_key() -> Optional[str]:
  """Gets the API key from environment variables, prioritizing GOOGLE_API_KEY.

  Returns:
      The API key string if found, otherwise None. Empty string is considered
      invalid.
  """
  env_google_api_key = os.environ.get('GOOGLE_API_KEY', None)
  env_gemini_api_key = os.environ.get('GEMINI_API_KEY', None)
  if env_google_api_key and env_gemini_api_key:
    logger.warning(
        'Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.'
    )

  return env_google_api_key or env_gemini_api_key or None


def _append_library_version_headers(headers: dict[str, str]) -> None:
  """Appends the telemetry header to the headers dict."""
  library_label = f'google-genai-sdk/{version.__version__}'
  language_label = 'gl-python/' + sys.version.split()[0]
  version_header_value = f'{library_label} {language_label}'
  if (
      'user-agent' in headers
      and version_header_value not in headers['user-agent']
  ):
    headers['user-agent'] = f'{version_header_value} ' + headers['user-agent']
  elif 'user-agent' not in headers:
    headers['user-agent'] = version_header_value
  if (
      'x-goog-api-client' in headers
      and version_header_value not in headers['x-goog-api-client']
  ):
    headers['x-goog-api-client'] = (
        f'{version_header_value} ' + headers['x-goog-api-client']
    )
  elif 'x-goog-api-client' not in headers:
    headers['x-goog-api-client'] = version_header_value


def _patch_http_options(
    options: HttpOptions, patch_options: HttpOptions
) -> HttpOptions:
  copy_option = options.model_copy()

  options_headers = copy_option.headers or {}
  patch_options_headers = patch_options.headers or {}
  copy_option.headers = {
      **options_headers,
      **patch_options_headers,
  }

  http_options_keys = HttpOptions.model_fields.keys()

  for key in http_options_keys:
    if key == 'headers':
      continue
    patch_value = getattr(patch_options, key, None)
    if patch_value is not None:
      setattr(copy_option, key, patch_value)
    else:
      setattr(copy_option, key, getattr(options, key))

  if copy_option.headers is not None:
    _append_library_version_headers(copy_option.headers)
  return copy_option


def _populate_server_timeout_header(
    headers: dict[str, str], timeout_in_seconds: Optional[Union[float, int]]
) -> None:
  """Populates the server timeout header in the headers dict."""
  if timeout_in_seconds and 'X-Server-Timeout' not in headers:
    headers['X-Server-Timeout'] = str(math.ceil(timeout_in_seconds))


def _join_url_path(base_url: str, path: str) -> str:
  parsed_base = urlparse(base_url)
  base_path = (
      parsed_base.path[:-1]
      if parsed_base.path.endswith('/')
      else parsed_base.path
  )
  path = path[1:] if path.startswith('/') else path
  return urlunparse(parsed_base._replace(path=base_path + '/' + path))


def _load_auth(*, project: Union[str, None]) -> Tuple[Credentials, str]:
  """Loads google auth credentials and project id."""
  credentials, loaded_project_id = google.auth.default(  # type: ignore[no-untyped-call]
      scopes=['https://www.googleapis.com/auth/cloud-platform'],
  )

  if not project:
    project = loaded_project_id

  if not project:
    raise ValueError(
        'Could not resolve project using application default credentials.'
    )

  return credentials, project


def _refresh_auth(credentials: Credentials) -> Credentials:
  credentials.refresh(Request())  # type: ignore[no-untyped-call]
  return credentials


def _get_timeout_in_seconds(
    timeout: Optional[Union[float, int]],
) -> Optional[float]:
  """Converts the timeout to seconds."""
  if timeout:
    # HttpOptions.timeout is in milliseconds. But httpx.Client.request()
    # expects seconds.
    timeout_in_seconds = timeout / 1000.0
  else:
    timeout_in_seconds = None
  return timeout_in_seconds


@dataclass
class HttpRequest:
  headers: dict[str, str]
  url: str
  method: str
  data: Union[dict[str, object], bytes]
  timeout: Optional[float] = None


class HttpResponse:

  def __init__(
      self,
      headers: Union[dict[str, str], httpx.Headers, 'CIMultiDictProxy[str]'],
      response_stream: Union[Any, str] = None,
      byte_stream: Union[Any, bytes] = None,
  ):
    self.status_code: int = 200
    self.headers = headers
    self.response_stream = response_stream
    self.byte_stream = byte_stream

  # Async iterator for async streaming.
  def __aiter__(self) -> 'HttpResponse':
    self.segment_iterator = self.async_segments()
    return self

  async def __anext__(self) -> Any:
    try:
      return await self.segment_iterator.__anext__()
    except StopIteration:
      raise StopAsyncIteration

  @property
  def json(self) -> Any:
    if not self.response_stream[0]:  # Empty response
      return ''
    return json.loads(self.response_stream[0])

  def segments(self) -> Generator[Any, None, None]:
    if isinstance(self.response_stream, list):
      # list of objects retrieved from replay or from non-streaming API.
      for chunk in self.response_stream:
        yield json.loads(chunk) if chunk else {}
    elif self.response_stream is None:
      yield from []
    else:
      # Iterator of objects retrieved from the API.
      for chunk in self.response_stream.iter_lines():  # type: ignore[union-attr]
        if chunk:
          # In streaming mode, the chunk of JSON is prefixed with "data:" which
          # we must strip before parsing.
          if not isinstance(chunk, str):
            chunk = chunk.decode('utf-8')
          if chunk.startswith('data: '):
            chunk = chunk[len('data: ') :]
          yield json.loads(chunk)

  async def async_segments(self) -> AsyncIterator[Any]:
    if isinstance(self.response_stream, list):
      # list of objects retrieved from replay or from non-streaming API.
      for chunk in self.response_stream:
        yield json.loads(chunk) if chunk else {}
    elif self.response_stream is None:
      async for c in []:  # type: ignore[attr-defined]
        yield c
    else:
      # Iterator of objects retrieved from the API.
      if hasattr(self.response_stream, 'aiter_lines'):
        async for chunk in self.response_stream.aiter_lines():
          # This is httpx.Response.
          if chunk:
            # In async streaming mode, the chunk of JSON is prefixed with
            # "data:" which we must strip before parsing.
            if not isinstance(chunk, str):
              chunk = chunk.decode('utf-8')
            if chunk.startswith('data: '):
              chunk = chunk[len('data: ') :]
            yield json.loads(chunk)
      elif hasattr(self.response_stream, 'content'):
        async for chunk in self.response_stream.content.iter_any():
          # This is aiohttp.ClientResponse.
          if chunk:
            # In async streaming mode, the chunk of JSON is prefixed with
            # "data:" which we must strip before parsing.
            if not isinstance(chunk, str):
              chunk = chunk.decode('utf-8')
            if chunk.startswith('data: '):
              chunk = chunk[len('data: ') :]
            yield json.loads(chunk)
      else:
        raise ValueError('Error parsing streaming response.')

  def byte_segments(self) -> Generator[Union[bytes, Any], None, None]:
    if isinstance(self.byte_stream, list):
      # list of objects retrieved from replay or from non-streaming API.
      yield from self.byte_stream
    elif self.byte_stream is None:
      yield from []
    else:
      raise ValueError(
          'Byte segments are not supported for streaming responses.'
      )

  def _copy_to_dict(self, response_payload: dict[str, object]) -> None:
    # Cannot pickle 'generator' object.
    delattr(self, 'segment_iterator')
    for attribute in dir(self):
      response_payload[attribute] = copy.deepcopy(getattr(self, attribute))


# Default retry options.
# The config is based on https://cloud.google.com/storage/docs/retry-strategy.
_RETRY_ATTEMPTS = 3
_RETRY_INITIAL_DELAY = 1.0  # seconds
_RETRY_MAX_DELAY = 120.0  # seconds
_RETRY_EXP_BASE = 2
_RETRY_JITTER = 1
_RETRY_HTTP_STATUS_CODES = (
    408,  # Request timeout.
    429,  # Too many requests.
    500,  # Internal server error.
    502,  # Bad gateway.
    503,  # Service unavailable.
    504,  # Gateway timeout
)


def _retry_args(options: Optional[HttpRetryOptions]) -> dict[str, Any]:
  """Returns the retry args for the given http retry options.

  Args:
    options: The http retry options to use for the retry configuration. If None,
      the 'never retry' stop strategy will be used.

  Returns:
    The arguments passed to the tenacity.(Async)Retrying constructor.
  """
  if options is None:
    return {'stop': tenacity.stop_after_attempt(1)}

  stop = tenacity.stop_after_attempt(options.attempts or _RETRY_ATTEMPTS)
  retriable_codes = options.http_status_codes or _RETRY_HTTP_STATUS_CODES
  retry = tenacity.retry_if_result(
      lambda response: response.status_code in retriable_codes,
  )
  retry_error_callback = lambda retry_state: retry_state.outcome.result()
  wait = tenacity.wait_exponential_jitter(
      initial=options.initial_delay or _RETRY_INITIAL_DELAY,
      max=options.max_delay or _RETRY_MAX_DELAY,
      exp_base=options.exp_base or _RETRY_EXP_BASE,
      jitter=options.jitter or _RETRY_JITTER,
  )
  return {
      'stop': stop,
      'retry': retry,
      'retry_error_callback': retry_error_callback,
      'wait': wait,
  }


class SyncHttpxClient(httpx.Client):
  """Sync httpx client."""

  def __init__(self, **kwargs: Any) -> None:
    """Initializes the httpx client."""
    kwargs.setdefault('follow_redirects', True)
    super().__init__(**kwargs)

  def __del__(self) -> None:
    """Closes the httpx client."""
    try:
      if self.is_closed:
        return
    except Exception:
      pass
    try:
      self.close()
    except Exception:
      pass


class AsyncHttpxClient(httpx.AsyncClient):
  """Async httpx client."""

  def __init__(self, **kwargs: Any) -> None:
    """Initializes the httpx client."""
    kwargs.setdefault('follow_redirects', True)
    super().__init__(**kwargs)

  def __del__(self) -> None:
    try:
      if self.is_closed:
        return
    except Exception:
      pass
    try:
      asyncio.get_running_loop().create_task(self.aclose())
    except Exception:
      pass


class BaseApiClient:
  """Client for calling HTTP APIs sending and receiving JSON."""

  def __init__(
      self,
      vertexai: Optional[bool] = None,
      api_key: Optional[str] = None,
      credentials: Optional[google.auth.credentials.Credentials] = None,
      project: Optional[str] = None,
      location: Optional[str] = None,
      http_options: Optional[HttpOptionsOrDict] = None,
  ):
    self.vertexai = vertexai
    if self.vertexai is None:
      if os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', '0').lower() in [
          'true',
          '1',
      ]:
        self.vertexai = True

    # Validate explicitly set initializer values.
    if (project or location) and api_key:
      # API cannot consume both project/location and api_key.
      raise ValueError(
          'Project/location and API key are mutually exclusive in the client'
          ' initializer.'
      )
    elif credentials and api_key:
      # API cannot consume both credentials and api_key.
      raise ValueError(
          'Credentials and API key are mutually exclusive in the client'
          ' initializer.'
      )

    # Validate http_options if it is provided.
    validated_http_options = HttpOptions()
    if isinstance(http_options, dict):
      try:
        validated_http_options = HttpOptions.model_validate(http_options)
      except ValidationError as e:
        raise ValueError('Invalid http_options') from e
    elif isinstance(http_options, HttpOptions):
      validated_http_options = http_options

    # Retrieve implicitly set values from the environment.
    env_project = os.environ.get('GOOGLE_CLOUD_PROJECT', None)
    env_location = os.environ.get('GOOGLE_CLOUD_LOCATION', None)
    env_api_key = _get_env_api_key()
    self.project = project or env_project
    self.location = location or env_location
    self.api_key = api_key or env_api_key

    self._credentials = credentials
    self._http_options = HttpOptions()
    # Initialize the lock. This lock will be used to protect access to the
    # credentials. This is crucial for thread safety when multiple coroutines
    # might be accessing the credentials at the same time.
    try:
      self._sync_auth_lock = threading.Lock()
      self._async_auth_lock = asyncio.Lock()
    except RuntimeError:
      asyncio.set_event_loop(asyncio.new_event_loop())
      self._sync_auth_lock = threading.Lock()
      self._async_auth_lock = asyncio.Lock()

    # Handle when to use Vertex AI in express mode (api key).
    # Explicit initializer arguments are already validated above.
    if self.vertexai:
      if credentials:
        # Explicit credentials take precedence over implicit api_key.
        logger.info(
            'The user provided Google Cloud credentials will take precedence'
            + ' over the API key from the environment variable.'
        )
        self.api_key = None
      elif (env_location or env_project) and api_key:
        # Explicit api_key takes precedence over implicit project/location.
        logger.info(
            'The user provided Vertex AI API key will take precedence over the'
            + ' project/location from the environment variables.'
        )
        self.project = None
        self.location = None
      elif (project or location) and env_api_key:
        # Explicit project/location takes precedence over implicit api_key.
        logger.info(
            'The user provided project/location will take precedence over the'
            + ' Vertex AI API key from the environment variable.'
        )
        self.api_key = None
      elif (env_location or env_project) and env_api_key:
        # Implicit project/location takes precedence over implicit api_key.
        logger.info(
            'The project/location from the environment variables will take'
            + ' precedence over the API key from the environment variables.'
        )
        self.api_key = None
      if not self.project and not self.api_key:
        credentials, self.project = _load_auth(project=None)
        if not self._credentials:
          self._credentials = credentials
      if not ((self.project and self.location) or self.api_key):
        raise ValueError(
            'Project and location or API key must be set when using the Vertex '
            'AI API.'
        )
      if self.api_key or self.location == 'global':
        self._http_options.base_url = f'https://aiplatform.googleapis.com/'
      else:
        self._http_options.base_url = (
            f'https://{self.location}-aiplatform.googleapis.com/'
        )
      self._http_options.api_version = 'v1beta1'
    else:  # Implicit initialization or missing arguments.
      if not self.api_key:
        raise ValueError(
            'Missing key inputs argument! To use the Google AI API,'
            ' provide (`api_key`) arguments. To use the Google Cloud API,'
            ' provide (`vertexai`, `project` & `location`) arguments.'
        )
      self._http_options.base_url = 'https://generativelanguage.googleapis.com/'
      self._http_options.api_version = 'v1beta'
    # Default options for both clients.
    self._http_options.headers = {'Content-Type': 'application/json'}
    if self.api_key:
      self.api_key = self.api_key.strip()
      if self._http_options.headers is not None:
        self._http_options.headers['x-goog-api-key'] = self.api_key
    # Update the http options with the user provided http options.
    if http_options:
      self._http_options = _patch_http_options(
          self._http_options, validated_http_options
      )
    else:
      if self._http_options.headers is not None:
        _append_library_version_headers(self._http_options.headers)

    client_args, async_client_args = self._ensure_httpx_ssl_ctx(
        self._http_options
    )
    self._httpx_client = SyncHttpxClient(**client_args)
    self._async_httpx_client = AsyncHttpxClient(**async_client_args)
    if has_aiohttp:
      # Do it once at the genai.Client level. Share among all requests.
      self._async_client_session_request_args = self._ensure_aiohttp_ssl_ctx(
          self._http_options
      )

    retry_kwargs = _retry_args(self._http_options.retry_options)
    self._retry = tenacity.Retrying(**retry_kwargs, reraise=True)
    self._async_retry = tenacity.AsyncRetrying(**retry_kwargs, reraise=True)

  @staticmethod
  def _ensure_httpx_ssl_ctx(
      options: HttpOptions,
  ) -> Tuple[dict[str, Any], dict[str, Any]]:
    """Ensures the SSL context is present in the HTTPX client args.

    Creates a default SSL context if one is not provided.

    Args:
      options: The http options to check for SSL context.

    Returns:
      A tuple of sync/async httpx client args.
    """

    verify = 'verify'
    args = options.client_args
    async_args = options.async_client_args
    ctx = (
        args.get(verify)
        if args
        else None or async_args.get(verify)
        if async_args
        else None
    )

    if not ctx:
      # Initialize the SSL context for the httpx client.
      # Unlike requests, the httpx package does not automatically pull in the
      # environment variables SSL_CERT_FILE or SSL_CERT_DIR. They need to be
      # enabled explicitly.
      ctx = ssl.create_default_context(
          cafile=os.environ.get('SSL_CERT_FILE', certifi.where()),
          capath=os.environ.get('SSL_CERT_DIR'),
      )

    def _maybe_set(
        args: Optional[dict[str, Any]],
        ctx: ssl.SSLContext,
    ) -> dict[str, Any]:
      """Sets the SSL context in the client args if not set.

      Does not override the SSL context if it is already set.

      Args:
        args: The client args to to check for SSL context.
        ctx: The SSL context to set.

      Returns:
        The client args with the SSL context included.
      """
      if not args or not args.get(verify):
        args = (args or {}).copy()
        args[verify] = ctx
      # Drop the args that isn't used by the httpx client.
      copied_args = args.copy()
      for key in copied_args.copy():
        if key not in inspect.signature(httpx.Client.__init__).parameters:
          del copied_args[key]
      return copied_args

    return (
        _maybe_set(args, ctx),
        _maybe_set(async_args, ctx),
    )

  @staticmethod
  def _ensure_aiohttp_ssl_ctx(options: HttpOptions) -> dict[str, Any]:
    """Ensures the SSL context is present in the async client args.

    Creates a default SSL context if one is not provided.

    Args:
      options: The http options to check for SSL context.

    Returns:
      An async aiohttp ClientSession._request args.
    """

    verify = 'ssl'  # keep it consistent with httpx.
    async_args = options.async_client_args
    ctx = async_args.get(verify) if async_args else None

    if not ctx:
      # Initialize the SSL context for the httpx client.
      # Unlike requests, the aiohttp package does not automatically pull in the
      # environment variables SSL_CERT_FILE or SSL_CERT_DIR. They need to be
      # enabled explicitly. Instead of 'verify' at client level in httpx,
      # aiohttp uses 'ssl' at request level.
      ctx = ssl.create_default_context(
          cafile=os.environ.get('SSL_CERT_FILE', certifi.where()),
          capath=os.environ.get('SSL_CERT_DIR'),
      )

    def _maybe_set(
        args: Optional[dict[str, Any]],
        ctx: ssl.SSLContext,
    ) -> dict[str, Any]:
      """Sets the SSL context in the client args if not set.

      Does not override the SSL context if it is already set.

      Args:
        args: The client args to to check for SSL context.
        ctx: The SSL context to set.

      Returns:
        The client args with the SSL context included.
      """
      if not args or not args.get(verify):
        args = (args or {}).copy()
        args[verify] = ctx
      # Drop the args that isn't in the aiohttp RequestOptions.
      copied_args = args.copy()
      for key in copied_args.copy():
        if (
            key
            not in inspect.signature(aiohttp.ClientSession._request).parameters
        ):
          del copied_args[key]
      return copied_args

    return _maybe_set(async_args, ctx)

  def _websocket_base_url(self) -> str:
    url_parts = urlparse(self._http_options.base_url)
    return url_parts._replace(scheme='wss').geturl()  # type: ignore[arg-type, return-value]

  def _access_token(self) -> str:
    """Retrieves the access token for the credentials."""
    with self._sync_auth_lock:
      if not self._credentials:
        self._credentials, project = _load_auth(project=self.project)
        if not self.project:
          self.project = project

      if self._credentials:
        if self._credentials.expired or not self._credentials.token:
          # Only refresh when it needs to. Default expiration is 3600 seconds.
          _refresh_auth(self._credentials)
        if not self._credentials.token:
          raise RuntimeError('Could not resolve API token from the environment')
        return self._credentials.token  # type: ignore[no-any-return]
      else:
        raise RuntimeError('Could not resolve API token from the environment')

  async def _async_access_token(self) -> Union[str, Any]:
    """Retrieves the access token for the credentials asynchronously."""
    if not self._credentials:
      async with self._async_auth_lock:
        # This ensures that only one coroutine can execute the auth logic at a
        # time for thread safety.
        if not self._credentials:
          # Double check that the credentials are not set before loading them.
          self._credentials, project = await asyncio.to_thread(
              _load_auth, project=self.project
          )
          if not self.project:
            self.project = project

    if self._credentials:
      if self._credentials.expired or not self._credentials.token:
        # Only refresh when it needs to. Default expiration is 3600 seconds.
        async with self._async_auth_lock:
          if self._credentials.expired or not self._credentials.token:
            # Double check that the credentials expired before refreshing.
            await asyncio.to_thread(_refresh_auth, self._credentials)

      if not self._credentials.token:
        raise RuntimeError('Could not resolve API token from the environment')

      return self._credentials.token
    else:
      raise RuntimeError('Could not resolve API token from the environment')

  def _build_request(
      self,
      http_method: str,
      path: str,
      request_dict: dict[str, object],
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> HttpRequest:
    # Remove all special dict keys such as _url and _query.
    keys_to_delete = [key for key in request_dict.keys() if key.startswith('_')]
    for key in keys_to_delete:
      del request_dict[key]
    # patch the http options with the user provided settings.
    if http_options:
      if isinstance(http_options, HttpOptions):
        patched_http_options = _patch_http_options(
            self._http_options,
            http_options,
        )
      else:
        patched_http_options = _patch_http_options(
            self._http_options, HttpOptions.model_validate(http_options)
        )
    else:
      patched_http_options = self._http_options
    # Skip adding project and locations when getting Vertex AI base models.
    query_vertex_base_models = False
    if (
        self.vertexai
        and http_method == 'get'
        and path.startswith('publishers/google/models')
    ):
      query_vertex_base_models = True
    if (
        self.vertexai
        and not path.startswith('projects/')
        and not query_vertex_base_models
        and not self.api_key
    ):
      path = f'projects/{self.project}/locations/{self.location}/' + path

    if patched_http_options.api_version is None:
      versioned_path = f'/{path}'
    else:
      versioned_path = f'{patched_http_options.api_version}/{path}'

    if (
        patched_http_options.base_url is None
        or not patched_http_options.base_url
    ):
      raise ValueError('Base URL must be set.')
    else:
      base_url = patched_http_options.base_url

    if (
        hasattr(patched_http_options, 'extra_body')
        and patched_http_options.extra_body
    ):
      _common.recursive_dict_update(
          request_dict, patched_http_options.extra_body
      )

    url = _join_url_path(
        base_url,
        versioned_path,
    )

    if self.api_key and self.api_key.startswith('auth_tokens/'):
      raise EphemeralTokenAPIKeyError(
          'Ephemeral tokens can only be used with the live API.'
      )

    timeout_in_seconds = _get_timeout_in_seconds(patched_http_options.timeout)

    if patched_http_options.headers is None:
      raise ValueError('Request headers must be set.')
    _populate_server_timeout_header(
        patched_http_options.headers, timeout_in_seconds
    )
    return HttpRequest(
        method=http_method,
        url=url,
        headers=patched_http_options.headers,
        data=request_dict,
        timeout=timeout_in_seconds,
    )

  def _request_once(
      self,
      http_request: HttpRequest,
      stream: bool = False,
  ) -> HttpResponse:
    data: Optional[Union[str, bytes]] = None
    if self.vertexai and not self.api_key:
      http_request.headers['Authorization'] = f'Bearer {self._access_token()}'
      if self._credentials and self._credentials.quota_project_id:
        http_request.headers['x-goog-user-project'] = (
            self._credentials.quota_project_id
        )
      data = json.dumps(http_request.data) if http_request.data else None
    else:
      if http_request.data:
        if not isinstance(http_request.data, bytes):
          data = json.dumps(http_request.data) if http_request.data else None
        else:
          data = http_request.data

    if stream:
      httpx_request = self._httpx_client.build_request(
          method=http_request.method,
          url=http_request.url,
          content=data,
          headers=http_request.headers,
          timeout=http_request.timeout,
      )
      response = self._httpx_client.send(httpx_request, stream=stream)
      errors.APIError.raise_for_response(response)
      return HttpResponse(
          response.headers, response if stream else [response.text]
      )
    else:
      response = self._httpx_client.request(
          method=http_request.method,
          url=http_request.url,
          headers=http_request.headers,
          content=data,
          timeout=http_request.timeout,
      )
      errors.APIError.raise_for_response(response)
      return HttpResponse(
          response.headers, response if stream else [response.text]
      )

  def _request(
      self,
      http_request: HttpRequest,
      stream: bool = False,
  ) -> HttpResponse:
    return self._retry(self._request_once, http_request, stream)  # type: ignore[no-any-return]

  async def _async_request_once(
      self, http_request: HttpRequest, stream: bool = False
  ) -> HttpResponse:
    data: Optional[Union[str, bytes]] = None
    if self.vertexai and not self.api_key:
      http_request.headers['Authorization'] = (
          f'Bearer {await self._async_access_token()}'
      )
      if self._credentials and self._credentials.quota_project_id:
        http_request.headers['x-goog-user-project'] = (
            self._credentials.quota_project_id
        )
      data = json.dumps(http_request.data) if http_request.data else None
    else:
      if http_request.data:
        if not isinstance(http_request.data, bytes):
          data = json.dumps(http_request.data) if http_request.data else None
        else:
          data = http_request.data

    if stream:
      if has_aiohttp:
        session = aiohttp.ClientSession(
            headers=http_request.headers,
            trust_env=True,
        )
        response = await session.request(
            method=http_request.method,
            url=http_request.url,
            headers=http_request.headers,
            data=data,
            timeout=aiohttp.ClientTimeout(connect=http_request.timeout),
            **self._async_client_session_request_args,
        )
        await errors.APIError.raise_for_async_response(response)
        return HttpResponse(response.headers, response)
      else:
        # aiohttp is not available. Fall back to httpx.
        httpx_request = self._async_httpx_client.build_request(
            method=http_request.method,
            url=http_request.url,
            content=data,
            headers=http_request.headers,
            timeout=http_request.timeout,
        )
        client_response = await self._async_httpx_client.send(
            httpx_request,
            stream=stream,
        )
        await errors.APIError.raise_for_async_response(client_response)
        return HttpResponse(client_response.headers, client_response)
    else:
      if has_aiohttp:
        async with aiohttp.ClientSession(
            headers=http_request.headers,
            trust_env=True,
        ) as session:
          response = await session.request(
              method=http_request.method,
              url=http_request.url,
              headers=http_request.headers,
              data=data,
              timeout=aiohttp.ClientTimeout(connect=http_request.timeout),
              **self._async_client_session_request_args,
          )
          await errors.APIError.raise_for_async_response(response)
          return HttpResponse(response.headers, [await response.text()])
      else:
        # aiohttp is not available. Fall back to httpx.
        client_response = await self._async_httpx_client.request(
            method=http_request.method,
            url=http_request.url,
            headers=http_request.headers,
            content=data,
            timeout=http_request.timeout,
        )
        await errors.APIError.raise_for_async_response(client_response)
        return HttpResponse(client_response.headers, [client_response.text])

  async def _async_request(
      self,
      http_request: HttpRequest,
      stream: bool = False,
  ) -> HttpResponse:
    return await self._async_retry(  # type: ignore[no-any-return]
        self._async_request_once, http_request, stream
    )

  def get_read_only_http_options(self) -> dict[str, Any]:
    if isinstance(self._http_options, BaseModel):
      copied = self._http_options.model_dump()
    else:
      copied = self._http_options
    return copied

  def request(
      self,
      http_method: str,
      path: str,
      request_dict: dict[str, object],
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> SdkHttpResponse:
    http_request = self._build_request(
        http_method, path, request_dict, http_options
    )
    response = self._request(http_request, stream=False)
    response_body = response.response_stream[0] if response.response_stream else ''
    return SdkHttpResponse(
        headers=response.headers, body=response_body
    )


  def request_streamed(
      self,
      http_method: str,
      path: str,
      request_dict: dict[str, object],
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> Generator[SdkHttpResponse, None, None]:
    http_request = self._build_request(
        http_method, path, request_dict, http_options
    )

    session_response = self._request(http_request, stream=True)
    for chunk in session_response.segments():
      yield SdkHttpResponse(headers=session_response.headers, body=json.dumps(chunk))

  async def async_request(
      self,
      http_method: str,
      path: str,
      request_dict: dict[str, object],
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> SdkHttpResponse:
    http_request = self._build_request(
        http_method, path, request_dict, http_options
    )

    result = await self._async_request(http_request=http_request, stream=False)
    response_body = result.response_stream[0] if result.response_stream else ''
    return SdkHttpResponse(
        headers=result.headers, body=response_body
    )


  async def async_request_streamed(
      self,
      http_method: str,
      path: str,
      request_dict: dict[str, object],
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> Any:
    http_request = self._build_request(
        http_method, path, request_dict, http_options
    )

    response = await self._async_request(http_request=http_request, stream=True)

    async def async_generator():  # type: ignore[no-untyped-def]
      async for chunk in response:
        yield SdkHttpResponse(headers=response.headers, body=json.dumps(chunk))

    return async_generator()  # type: ignore[no-untyped-call]

  def upload_file(
      self,
      file_path: Union[str, io.IOBase],
      upload_url: str,
      upload_size: int,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> HttpResponse:
    """Transfers a file to the given URL.

    Args:
      file_path: The full path to the file or a file like object inherited from
        io.BytesIO. If the local file path is not found, an error will be
        raised.
      upload_url: The URL to upload the file to.
      upload_size: The size of file content to be uploaded, this will have to
        match the size requested in the resumable upload request.
      http_options: The http options to use for the request.

    returns:
          The HttpResponse object from the finalize request.
    """
    if isinstance(file_path, io.IOBase):
      return self._upload_fd(
          file_path, upload_url, upload_size, http_options=http_options
      )
    else:
      with open(file_path, 'rb') as file:
        return self._upload_fd(
            file, upload_url, upload_size, http_options=http_options
        )

  def _upload_fd(
      self,
      file: io.IOBase,
      upload_url: str,
      upload_size: int,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> HttpResponse:
    """Transfers a file to the given URL.

    Args:
      file: A file like object inherited from io.BytesIO.
      upload_url: The URL to upload the file to.
      upload_size: The size of file content to be uploaded, this will have to
        match the size requested in the resumable upload request.
      http_options: The http options to use for the request.

    returns:
          The HttpResponse object from the finalize request.
    """
    offset = 0
    # Upload the file in chunks
    while True:
      file_chunk = file.read(CHUNK_SIZE)
      chunk_size = 0
      if file_chunk:
        chunk_size = len(file_chunk)
      upload_command = 'upload'
      # If last chunk, finalize the upload.
      if chunk_size + offset >= upload_size:
        upload_command += ', finalize'
      http_options = http_options if http_options else self._http_options
      timeout = (
          http_options.get('timeout')
          if isinstance(http_options, dict)
          else http_options.timeout
      )
      if timeout is None:
        # Per request timeout is not configured. Check the global timeout.
        timeout = (
            self._http_options.timeout
            if isinstance(self._http_options, dict)
            else self._http_options.timeout
        )
      timeout_in_seconds = _get_timeout_in_seconds(timeout)
      upload_headers = {
          'X-Goog-Upload-Command': upload_command,
          'X-Goog-Upload-Offset': str(offset),
          'Content-Length': str(chunk_size),
      }
      _populate_server_timeout_header(upload_headers, timeout_in_seconds)
      retry_count = 0
      while retry_count < MAX_RETRY_COUNT:
        response = self._httpx_client.request(
            method='POST',
            url=upload_url,
            headers=upload_headers,
            content=file_chunk,
            timeout=timeout_in_seconds,
        )
        if response.headers.get('x-goog-upload-status'):
          break
        delay_seconds = INITIAL_RETRY_DELAY * (DELAY_MULTIPLIER**retry_count)
        retry_count += 1
        time.sleep(delay_seconds)

      offset += chunk_size
      if response.headers.get('x-goog-upload-status') != 'active':
        break  # upload is complete or it has been interrupted.
      if upload_size <= offset:  # Status is not finalized.
        raise ValueError(
            f'All content has been uploaded, but the upload status is not'
            f' finalized.'
        )

    if response.headers.get('x-goog-upload-status') != 'final':
      raise ValueError('Failed to upload file: Upload status is not finalized.')
    return HttpResponse(response.headers, response_stream=[response.text])

  def download_file(
      self,
      path: str,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> Union[Any, bytes]:
    """Downloads the file data.

    Args:
      path: The request path with query params.
      http_options: The http options to use for the request.

    returns:
          The file bytes
    """
    http_request = self._build_request(
        'get', path=path, request_dict={}, http_options=http_options
    )

    data: Optional[Union[str, bytes]] = None
    if http_request.data:
      if not isinstance(http_request.data, bytes):
        data = json.dumps(http_request.data)
      else:
        data = http_request.data

    response = self._httpx_client.request(
        method=http_request.method,
        url=http_request.url,
        headers=http_request.headers,
        content=data,
        timeout=http_request.timeout,
    )

    errors.APIError.raise_for_response(response)
    return HttpResponse(
        response.headers, byte_stream=[response.read()]
    ).byte_stream[0]

  async def async_upload_file(
      self,
      file_path: Union[str, io.IOBase],
      upload_url: str,
      upload_size: int,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> HttpResponse:
    """Transfers a file asynchronously to the given URL.

    Args:
      file_path: The full path to the file. If the local file path is not found,
        an error will be raised.
      upload_url: The URL to upload the file to.
      upload_size: The size of file content to be uploaded, this will have to
        match the size requested in the resumable upload request.
      http_options: The http options to use for the request.

    returns:
          The HttpResponse object from the finalize request.
    """
    if isinstance(file_path, io.IOBase):
      return await self._async_upload_fd(
          file_path, upload_url, upload_size, http_options=http_options
      )
    else:
      file = anyio.Path(file_path)
      fd = await file.open('rb')
      async with fd:
        return await self._async_upload_fd(
            fd, upload_url, upload_size, http_options=http_options
        )

  async def _async_upload_fd(
      self,
      file: Union[io.IOBase, anyio.AsyncFile[Any]],
      upload_url: str,
      upload_size: int,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> HttpResponse:
    """Transfers a file asynchronously to the given URL.

    Args:
      file: A file like object inherited from io.BytesIO.
      upload_url: The URL to upload the file to.
      upload_size: The size of file content to be uploaded, this will have to
        match the size requested in the resumable upload request.
      http_options: The http options to use for the request.

    returns:
          The HttpResponse object from the finalized request.
    """
    offset = 0
    # Upload the file in chunks
    if has_aiohttp:  # pylint: disable=g-import-not-at-top
      async with aiohttp.ClientSession(
          headers=self._http_options.headers,
          trust_env=True,
      ) as session:
        while True:
          if isinstance(file, io.IOBase):
            file_chunk = file.read(CHUNK_SIZE)
          else:
            file_chunk = await file.read(CHUNK_SIZE)
          chunk_size = 0
          if file_chunk:
            chunk_size = len(file_chunk)
          upload_command = 'upload'
          # If last chunk, finalize the upload.
          if chunk_size + offset >= upload_size:
            upload_command += ', finalize'
          http_options = http_options if http_options else self._http_options
          timeout = (
              http_options.get('timeout')
              if isinstance(http_options, dict)
              else http_options.timeout
          )
          if timeout is None:
            # Per request timeout is not configured. Check the global timeout.
            timeout = (
                self._http_options.timeout
                if isinstance(self._http_options, dict)
                else self._http_options.timeout
            )
          timeout_in_seconds = _get_timeout_in_seconds(timeout)
          upload_headers = {
              'X-Goog-Upload-Command': upload_command,
              'X-Goog-Upload-Offset': str(offset),
              'Content-Length': str(chunk_size),
          }
          _populate_server_timeout_header(upload_headers, timeout_in_seconds)

          retry_count = 0
          response = None
          while retry_count < MAX_RETRY_COUNT:
            response = await session.request(
                method='POST',
                url=upload_url,
                data=file_chunk,
                headers=upload_headers,
                timeout=aiohttp.ClientTimeout(connect=timeout_in_seconds),
            )

            if response.headers.get('X-Goog-Upload-Status'):
              break
            delay_seconds = INITIAL_RETRY_DELAY * (
                DELAY_MULTIPLIER**retry_count
            )
            retry_count += 1
            time.sleep(delay_seconds)

          offset += chunk_size
          if (
              response is not None
              and response.headers.get('X-Goog-Upload-Status') != 'active'
          ):
            break  # upload is complete or it has been interrupted.

          if upload_size <= offset:  # Status is not finalized.
            raise ValueError(
                f'All content has been uploaded, but the upload status is not'
                f' finalized.'
            )
        if (
            response is not None
            and response.headers.get('X-Goog-Upload-Status') != 'final'
        ):
          raise ValueError(
              'Failed to upload file: Upload status is not finalized.'
          )
        return HttpResponse(
            response.headers, response_stream=[await response.text()]
        )
    else:
      # aiohttp is not available. Fall back to httpx.
      while True:
        if isinstance(file, io.IOBase):
          file_chunk = file.read(CHUNK_SIZE)
        else:
          file_chunk = await file.read(CHUNK_SIZE)
        chunk_size = 0
        if file_chunk:
          chunk_size = len(file_chunk)
        upload_command = 'upload'
        # If last chunk, finalize the upload.
        if chunk_size + offset >= upload_size:
          upload_command += ', finalize'
        http_options = http_options if http_options else self._http_options
        timeout = (
            http_options.get('timeout')
            if isinstance(http_options, dict)
            else http_options.timeout
        )
        if timeout is None:
          # Per request timeout is not configured. Check the global timeout.
          timeout = (
              self._http_options.timeout
              if isinstance(self._http_options, dict)
              else self._http_options.timeout
          )
        timeout_in_seconds = _get_timeout_in_seconds(timeout)
        upload_headers = {
            'X-Goog-Upload-Command': upload_command,
            'X-Goog-Upload-Offset': str(offset),
            'Content-Length': str(chunk_size),
        }
        _populate_server_timeout_header(upload_headers, timeout_in_seconds)

        retry_count = 0
        client_response = None
        while retry_count < MAX_RETRY_COUNT:
          client_response = await self._async_httpx_client.request(
              method='POST',
              url=upload_url,
              content=file_chunk,
              headers=upload_headers,
              timeout=timeout_in_seconds,
          )
          if (
              client_response is not None
              and client_response.headers
              and client_response.headers.get('x-goog-upload-status')
          ):
            break
          delay_seconds = INITIAL_RETRY_DELAY * (DELAY_MULTIPLIER**retry_count)
          retry_count += 1
          time.sleep(delay_seconds)

        offset += chunk_size
        if (
            client_response is not None
            and client_response.headers.get('x-goog-upload-status') != 'active'
        ):
          break  # upload is complete or it has been interrupted.

        if upload_size <= offset:  # Status is not finalized.
          raise ValueError(
              'All content has been uploaded, but the upload status is not'
              ' finalized.'
          )
      if (
          client_response is not None
          and client_response.headers.get('x-goog-upload-status') != 'final'
      ):
        raise ValueError(
            'Failed to upload file: Upload status is not finalized.'
        )
      return HttpResponse(
          client_response.headers, response_stream=[client_response.text]
      )

  async def async_download_file(
      self,
      path: str,
      *,
      http_options: Optional[HttpOptionsOrDict] = None,
  ) -> Union[Any, bytes]:
    """Downloads the file data.

    Args:
      path: The request path with query params.
      http_options: The http options to use for the request.

    returns:
          The file bytes
    """
    http_request = self._build_request(
        'get', path=path, request_dict={}, http_options=http_options
    )

    data: Optional[Union[str, bytes]] = None
    if http_request.data:
      if not isinstance(http_request.data, bytes):
        data = json.dumps(http_request.data)
      else:
        data = http_request.data

    if has_aiohttp:
      async with aiohttp.ClientSession(
          headers=http_request.headers,
          trust_env=True,
      ) as session:
        response = await session.request(
            method=http_request.method,
            url=http_request.url,
            headers=http_request.headers,
            data=data,
            timeout=aiohttp.ClientTimeout(connect=http_request.timeout),
        )
        await errors.APIError.raise_for_async_response(response)

        return HttpResponse(
            response.headers, byte_stream=[await response.read()]
        ).byte_stream[0]
    else:
      # aiohttp is not available. Fall back to httpx.
      client_response = await self._async_httpx_client.request(
          method=http_request.method,
          url=http_request.url,
          headers=http_request.headers,
          content=data,
          timeout=http_request.timeout,
      )
      await errors.APIError.raise_for_async_response(client_response)

      return HttpResponse(
          client_response.headers, byte_stream=[client_response.read()]
      ).byte_stream[0]

  # This method does nothing in the real api client. It is used in the
  # replay_api_client to verify the response from the SDK method matches the
  # recorded response.
  def _verify_response(self, response_model: _common.BaseModel) -> None:
    pass
