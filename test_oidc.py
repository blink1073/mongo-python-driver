import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue

from requests_oauth2client import AuthorizationRequest, OAuth2Client

auth_data = dict(
    authorizeEndpoint="https://corp.mongodb.com/oauth2/v1/authorize",
    tokenEndpoint="https://corp.mongodb.com/oauth2/v1/token",
    issuer="https://corp.mongodb.com",
    clientId="0oadp0hpl7q3UIehP297",
    clientSecret="9QPzHg42auUAutVl3SQeQDIJ6XlJZb19sEwcDUSd",
)


PORT = 8888
REDIRECT_URI = f"http://localhost:{PORT}/authorization-code/callback"
RESPONSE_QUEUE = Queue()


class MyRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        RESPONSE_QUEUE.put(self.path)
        self.send_response(200)


def run_server():
    server = HTTPServer(("localhost", PORT), MyRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    server.server_close()


# Start a server on 8888 and expose a callback endpoint
# the tunnel address will be different


def get_auth_token(auth_data):
    client_id = auth_data["clientId"]
    client_secret = auth_data["clientSecret"]
    token_endpoint = auth_data["tokenEndpoint"]
    authorization_endpoint = auth_data["authorizeEndpoint"]
    request = AuthorizationRequest(
        authorization_endpoint,
        client_id,
        scope="openid",
        redirect_uri=REDIRECT_URI,
        code_challenge_method="S256",
    )
    webbrowser.open(str(request))
    response_uri = RESPONSE_QUEUE.get()
    response = request.validate_callback(response_uri)
    client = OAuth2Client(token_endpoint, auth=(client_id, client_secret))
    token_response = client.token_request(
        {
            "grant_type": "authorization_code",
            "code": response.code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": response.code_verifier,
        }
    )
    return token_response.access_token


thread = threading.Thread(target=run_server, daemon=True)
thread.start()
print(get_auth_token(auth_data))
