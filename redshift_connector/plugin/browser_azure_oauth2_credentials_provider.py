import logging
import socket
import typing
from enum import Enum

from redshift_connector.error import InterfaceError
from redshift_connector.plugin.credential_provider_constants import azure_headers
from redshift_connector.plugin.jwt_credentials_provider import JwtCredentialsProvider
from redshift_connector.redshift_property import RedshiftProperty

if typing.TYPE_CHECKING:
    from redshift_connector.plugin.i_native_plugin import INativePlugin

logging.getLogger(__name__).addHandler(logging.NullHandler())
_logger: logging.Logger = logging.getLogger(__name__)


class BrowserAzureOAuth2CredentialsProvider(JwtCredentialsProvider):
    """
    Class to get JWT Token from any IDP using OAuth 2.0 API
    """

    class OAuthParamNames(Enum):
        """
        Defines OAuth parameter names used when requesting JWT token from the IdP
        """

        STATE: str = "state"
        REDIRECT: str = "redirect_uri"
        IDP_CODE: str = "code"
        CLIENT_ID: str = "client_id"
        RESPONSE_TYPE: str = "response_type"
        REQUESTED_TOKEN_TYPE: str = "requested_token_type"
        GRANT_TYPE: str = "grant_type"
        SCOPE: str = "scope"
        RESPONSE_MODE: str = "response_mode"
        RESOURCE: str = "resource"

    MICROSOFT_IDP_HOST: str = "login.microsoftonline.com"
    CURRENT_INTERACTION_SCHEMA: str = "https"

    def __init__(self: "BrowserAzureOAuth2CredentialsProvider") -> None:
        super().__init__()
        self.idp_tenant: typing.Optional[str] = None
        self.client_id: typing.Optional[str] = None
        self.scope: str = ""
        self.idp_response_timeout: int = 120
        self.listen_port: int = 0

    def add_parameter(
        self: "BrowserAzureOAuth2CredentialsProvider",
        info: RedshiftProperty,
    ) -> None:
        super().add_parameter(info)
        self.idp_tenant = info.idp_tenant
        self.client_id = info.client_id
        self.scope = info.scope

        if info.idp_response_timeout:
            self.idp_response_timeout = info.idp_response_timeout

        if info.listen_port:
            self.listen_port = info.listen_port

    def check_required_parameters(self: "BrowserAzureOAuth2CredentialsProvider") -> None:
        super().check_required_parameters()
        if not self.idp_tenant:
            raise InterfaceError("BrowserAzureOauth2CredentialsProvider requires idp_tenant")
        if not self.client_id:
            raise InterfaceError("BrowserAzureOauth2CredentialsProvider requires client_id")
        if not self.idp_response_timeout or self.idp_response_timeout < 10:
            raise InterfaceError(
                "BrowserAzureOauth2CredentialsProvider requires idp_response_timeout to be 10 seconds or greater"
            )

    def get_jwt_assertion(self: "BrowserAzureOAuth2CredentialsProvider") -> str:
        self.check_required_parameters()

        if self.listen_port == 0:
            _logger.debug("Listen port set to 0. Will pick random port")

        token: str = self.fetch_authorization_token()
        content: str = self.fetch_jwt_response(token)
        jwt_assertion: str = self.extract_jwt_assertion(content)
        return jwt_assertion

    def get_cache_key(self: "BrowserAzureOAuth2CredentialsProvider") -> str:
        return "{}{}".format(self.idp_tenant if self.idp_tenant else "", self.client_id if self.client_id else "")

    def run_server(
        self: "BrowserAzureOAuth2CredentialsProvider",
        listen_socket: socket.socket,
        idp_response_timeout: int,
        state: int,
    ):
        """
        Runs a server on localhost to listen for the IdP's response to our HTTP POST request for JWT assertion.

        Parameters
        ----------

        :param listen_socket: socket.socket
            The socket on which the method listens for a response
        :param idp_response_timeout: int
            The maximum time to listen on the socket, specified in seconds
        :param state: str
            The state generated by the client. This must match the state received from the IdP server

        Returns
        -------
        The IdP's response, including JWT assertion
        """
        conn, addr = listen_socket.accept()
        conn.settimeout(float(idp_response_timeout))
        size: int = 102400
        with conn:
            while True:
                part: bytes = conn.recv(size)
                decoded_part = part.decode()
                state_idx: int = decoded_part.find(
                    "{}=".format(BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.STATE.value)
                )

                if state_idx > -1:
                    received_state: str = decoded_part[state_idx + 6 : decoded_part.find("&", state_idx)]

                    if received_state != state:
                        raise InterfaceError(
                            "Incoming state {received} does not match the outgoing state {expected}".format(
                                received=received_state, expected=state
                            )
                        )

                    code_idx: int = decoded_part.find(
                        "{}=".format(BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.IDP_CODE.value)
                    )

                    if code_idx < 0:
                        raise InterfaceError("No code found")
                    received_code: str = decoded_part[code_idx + 5 : state_idx - 1]

                    if received_code == "":
                        raise InterfaceError("No valid code found")
                    conn.send(self.close_window_http_resp())
                    return received_code

    def open_browser(self: "BrowserAzureOAuth2CredentialsProvider", state: str) -> None:
        """
        Opens the default browser to allow user authentication with the IdP

        Parameters
        ----------
        :param state: str
            The state generated by the client

        Returns
        -------
        None
        """
        import webbrowser

        url: str = self.get_authorization_token_url(state=state)

        _logger.debug("SSO URI: {}".format(url))

        if url is None:
            raise InterfaceError("the login_url could not be empty")
        self.validate_url(url)
        webbrowser.open(url)

    def get_listen_socket(self: "BrowserAzureOAuth2CredentialsProvider") -> socket.socket:
        """
        Returns a listen socket used for user authentication
        """
        s: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))  # bind to any free port
        s.listen()
        self.listen_port = s.getsockname()[1]
        return s

    def get_authorization_token_url(self, state: str) -> str:
        """
        Returns a URL used for requesting authentication token from IdP
        """
        from urllib.parse import urlencode, urlunsplit

        params: typing.Dict[str, str] = {
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.RESPONSE_TYPE.value: "code",
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.RESPONSE_MODE.value: "form_post",
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.CLIENT_ID.value: typing.cast(str, self.client_id),
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.REDIRECT.value: self.redirectUri,
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.STATE.value: state,
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.SCOPE.value: "openid {}".format(self.scope),
        }

        encoded_params: str = urlencode(params)

        return urlunsplit(
            (
                BrowserAzureOAuth2CredentialsProvider.CURRENT_INTERACTION_SCHEMA,
                BrowserAzureOAuth2CredentialsProvider.MICROSOFT_IDP_HOST,
                "/{}/oauth2/v2.0/authorize".format(self.idp_tenant),
                encoded_params,
                "",
            )
        )

    def fetch_authorization_token(self: "BrowserAzureOAuth2CredentialsProvider") -> str:
        """
        Returns authorization token retrieved from IdP following user authentication in web browser.
        """
        import concurrent
        import random
        import socket

        alphabet: str = "abcdefghijklmnopqrstuvwxyz"
        state: str = "".join(random.sample(alphabet, 10))
        listen_socket: socket.socket = self.get_listen_socket()
        self.redirectUri = "http://localhost:{port}/redshift/".format(port=self.listen_port)
        try:
            return_value: str = ""
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(self.run_server, listen_socket, self.idp_response_timeout, state)
                self.open_browser(state)
                return_value = future.result()

            return str(return_value)
        except socket.error as e:
            _logger.error("Socket error: %s", e)
            raise e
        except Exception as e:
            _logger.error("Other Exception: %s", e)
            raise e
        finally:
            listen_socket.close()

    def get_jwt_post_request_url(self: "BrowserAzureOAuth2CredentialsProvider") -> str:
        """
        Returns URL used for sending HTTP POST request to retrieve JWT assertion.
        """
        return "{}://{}{}".format(
            BrowserAzureOAuth2CredentialsProvider.CURRENT_INTERACTION_SCHEMA,
            BrowserAzureOAuth2CredentialsProvider.MICROSOFT_IDP_HOST,
            "/{}/oauth2/v2.0/token".format(self.idp_tenant),
        )

    def fetch_jwt_response(self: "BrowserAzureOAuth2CredentialsProvider", token: str) -> str:
        """
        Returns JWT Response from IdP POST request.
        """
        import requests

        url: str = self.get_jwt_post_request_url()

        params: typing.Dict[str, str] = {
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.IDP_CODE.value: token,
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.GRANT_TYPE.value: "authorization_code",
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.SCOPE.value: typing.cast(str, self.scope),
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.CLIENT_ID.value: typing.cast(str, self.client_id),
            BrowserAzureOAuth2CredentialsProvider.OAuthParamNames.REDIRECT.value: self.redirectUri,
        }

        response: requests.Response = requests.post(
            url, data=params, headers=azure_headers, verify=self.do_verify_ssl_cert()
        )
        response.raise_for_status()
        return response.text

    def extract_jwt_assertion(self: "BrowserAzureOAuth2CredentialsProvider", content: str) -> str:
        """
        Returns encoded JWT assertion extracted from IdP response content
        """
        import json

        response_content: typing.Dict[str, str] = json.loads(content)

        if "access_token" not in response_content:
            raise InterfaceError("Failed to find access_token")

        encoded_jwt_assertion: str = response_content["access_token"]

        if not encoded_jwt_assertion:
            raise InterfaceError("Invalid access_token value")

        return encoded_jwt_assertion

    def get_idp_token(self: "BrowserAzureOAuth2CredentialsProvider") -> str:
        return super().get_idp_token()

    def get_sub_type(self: "BrowserAzureOAuth2CredentialsProvider") -> int:
        return super().get_sub_type()
