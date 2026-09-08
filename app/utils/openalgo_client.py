"""
Extended OpenAlgo API client with additional methods
"""
import httpx
from openalgo import api


class ExtendedOpenAlgoAPI(api):
    """Extended OpenAlgo API client with ping method and optimized timeout"""

    def __init__(self, api_key, host="http://127.0.0.1:5000", version="v1", ws_port=8765, ws_url=None, timeout=30):
        """
        Initialize with a 30 second timeout (default).
        Uses keyword args for super().__init__() because openalgo>=1.0.50
        changed the api.__init__ signature (timeout is now the 4th positional
        arg, displacing ws_port).
        """
        super().__init__(
            api_key=api_key,
            host=host,
            version=version,
            timeout=timeout,
            ws_port=ws_port,
            ws_url=ws_url,
        )

    def _make_request(self, endpoint, payload):
        """Override to guarantee timeout is applied regardless of SDK version.

        openalgo>=2.0 keeps a single connection-pooled httpx.Client on the
        instance. Reuse it when present: the module-level httpx.post opens and
        tears down a fresh TCP connection per call, which leaves thousands of
        sockets in TIME_WAIT over a trading session and eventually exhausts
        ephemeral ports. Falls back to httpx.post on older SDKs so the app still
        runs if the code deploys before the venv is upgraded.
        """
        url = self.base_url + endpoint
        pooled = getattr(self, 'client', None)
        try:
            if pooled is not None:
                response = pooled.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            else:
                response = httpx.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            return self._handle_response(response)
        except httpx.TimeoutException:
            return {
                'status': 'error',
                'message': f'Request timed out after {self.timeout}s. The server took too long to respond.',
                'error_type': 'timeout_error'
            }
        except httpx.ConnectError:
            return {
                'status': 'error',
                'message': 'Failed to connect to the server. Please check if the server is running.',
                'error_type': 'connection_error'
            }
        except httpx.HTTPError as e:
            return {
                'status': 'error',
                'message': f'HTTP error occurred: {str(e)}',
                'error_type': 'http_error'
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': f'An unexpected error occurred: {str(e)}',
                'error_type': 'unknown_error'
            }

    def gttorderbook(self, status=None, **kwargs):
        """
        List GTT triggers for this account.

        OpenAlgo defaults to active triggers only, which is why a fired GTT
        looks like it simply vanished. Pass status='all' to get the terminal
        states too (triggered, cancelled, expired, rejected), which is what
        closing the GTT lifecycle needs.

        openalgo>=2.0.4 ships GTTAPI natively, so defer to it when present and
        fall back to a raw post otherwise, since production may still be on an
        older SDK when this code deploys.

        Returns:
            dict: {'status': 'success', 'data': [...]} where each entry carries
            trigger_id, trigger_type, status, symbol, exchange, trigger_prices,
            last_price and legs.
        """
        native = getattr(super(), 'gttorderbook', None)
        if callable(native):
            if status is not None:
                kwargs['status'] = status
            return native(**kwargs)

        payload = {'apikey': self.api_key}
        if status is not None:
            payload['status'] = status
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return self._make_request('gttorderbook', payload)

    def ping(self):
        """
        Test connectivity and validate API key authentication
        
        This endpoint checks connectivity and validates the API key 
        authentication with the OpenAlgo platform.
        
        Returns:
            dict: Response with status, broker info, and message
            
        Example Response:
            {
                "data": {
                    "broker": "upstox",
                    "message": "pong"
                },
                "status": "success"
            }
        """
        payload = {"apikey": self.api_key}
        return self._make_request("ping", payload)