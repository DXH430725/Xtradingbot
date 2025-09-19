import base64
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest

try:
    from nacl.signing import SigningKey
    from nacl.encoding import RawEncoder
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False
    SigningKey = None
    RawEncoder = None

class BackpackPerpetualAuth(AuthBase):
    """
    Auth class for Backpack perpetual trading using ED25519 keypair authentication
    """

    def __init__(self, 
                 private_key: str, 
                 public_key: str, 
                 time_provider: TimeSynchronizer):
        """
        Initialize Backpack authentication
        
        :param private_key: Base64 encoded ED25519 private key
        :param public_key: Base64 encoded ED25519 public key 
        :param time_provider: Time synchronizer for timestamps
        """
        self.private_key = private_key
        self.public_key = public_key
        self.time_provider = time_provider
        
        if not NACL_AVAILABLE:
            raise ImportError("PyNaCl is required for Backpack authentication. Please install it with: pip install PyNaCl")
        
        # Initialize the signing key from the private key
        try:
            private_key_bytes = base64.b64decode(private_key)
            self.signing_key = SigningKey(private_key_bytes)
        except Exception as e:
            raise ValueError(f"Invalid private key format: {e}")

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds authentication headers to REST API requests
        
        :param request: The request to authenticate
        :return: Authenticated request
        """
        if self._requires_authentication(request):
            headers = self._get_auth_headers(request)
            if request.headers:
                request.headers.update(headers)
            else:
                request.headers = headers
                
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        Backpack WebSocket authentication (if needed)
        Currently Backpack may not require WebSocket auth for private streams
        """
        return request

    def _requires_authentication(self, request: RESTRequest) -> bool:
        """Check if the request requires authentication based on the path"""
        return request.url and "/wapi/" in request.url

    def _get_auth_headers(self, request: RESTRequest) -> Dict[str, str]:
        """
        Generate authentication headers for Backpack API requests
        
        :param request: The REST request to authenticate
        :return: Dictionary of authentication headers
        """
        timestamp = int(self.time_provider.time() * 1000)
        window = CONSTANTS.X_API_WINDOW_DEFAULT
        
        # Prepare parameters for signature
        params_dict = {}
        if request.params:
            params_dict.update(request.params)
        
        if request.method == RESTMethod.POST and request.data:
            if isinstance(request.data, str):
                try:
                    data_dict = json.loads(request.data)
                    params_dict.update(data_dict)
                except json.JSONDecodeError:
                    pass
            elif isinstance(request.data, dict):
                params_dict.update(request.data)
        
        # Generate signature
        signature = self._generate_signature(
            instruction=self._get_instruction_from_url(request.url),
            params_dict=params_dict,
            timestamp=timestamp,
            window=window
        )
        
        return {
            "X-API-Key": self.public_key,
            "X-Timestamp": str(timestamp),
            "X-Window": str(window),
            "X-Signature": signature,
        }

    def _get_instruction_from_url(self, url: str) -> str:
        """
        Extract the instruction from the URL for signature generation
        
        :param url: The request URL
        :return: Instruction string for signing
        """
        if "/wapi/v1/order" in url and url.endswith("/order"):
            return "orderExecute"
        elif "/wapi/v1/orders" in url:
            return "orderQueryAll"
        elif "/wapi/v1/balances" in url:
            return "balanceQuery"
        elif "/wapi/v1/positions" in url:
            return "positionQuery"
        elif "/wapi/v1/fills" in url:
            return "fillHistoryQuery"
        elif "/wapi/v1/deposits" in url:
            return "depositQueryAll"
        elif "/wapi/v1/withdrawals" in url:
            return "withdrawalQueryAll"
        elif "/wapi/v1/account" in url:
            return "accountQuery"
        else:
            # Default instruction - may need to be adjusted based on Backpack API
            return "orderExecute"

    def _generate_signature(self,
                          instruction: str,
                          params_dict: Dict[str, Any],
                          timestamp: int,
                          window: int) -> str:
        """
        Generate ED25519 signature for Backpack API requests
        
        :param instruction: The API instruction type
        :param params_dict: Request parameters
        :param timestamp: Request timestamp
        :param window: Request window
        :return: Base64 encoded signature
        """
        # Sort parameters alphabetically and create query string
        sorted_params = sorted(params_dict.items()) if params_dict else []
        params_string = "&".join([f"{k}={v}" for k, v in sorted_params])
        
        # Build the message to sign according to Backpack specification
        if params_string:
            message_to_sign = f"instruction={instruction}&{params_string}&timestamp={timestamp}&window={window}"
        else:
            message_to_sign = f"instruction={instruction}&timestamp={timestamp}&window={window}"
        
        # Sign the message using ED25519
        message_bytes = message_to_sign.encode('utf-8')
        signed = self.signing_key.sign(message_bytes, encoder=RawEncoder)
        signature = signed.signature
        
        # Return base64 encoded signature
        return base64.b64encode(signature).decode('utf-8')

    def get_referral_code_headers(self) -> Dict[str, str]:
        """
        Backpack referral headers (if supported)
        Currently returns empty dict as Backpack referral system is not documented
        """
        return {}