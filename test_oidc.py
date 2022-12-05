import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue

from requests_oauth2client import AuthorizationRequest, OAuth2Client

from pymongo import MongoClient

auth_data = dict(
    authorizeEndpoint="https://corp.mongodb.com/oauth2/v1/authorize",
    tokenEndpoint="https://corp.mongodb.com/oauth2/v1/token",
    issuer="https://corp.mongodb.com",
    clientId="0oadp0hpl7q3UIehP297",
    clientSecret="9QPzHg42auUAutVl3SQeQDIJ6XlJZb19sEwcDUSd",
)


LOCAL_PORT = 8888
REDIRECT_URI = f"http://localhost:{LOCAL_PORT}/authorization-code/callback"
RESPONSE_QUEUE = Queue()
INIT_CALLED = 0
REFRESH_CALLED = 0
TOKEN_AUTH = None


class MyRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        RESPONSE_QUEUE.put(self.path)
        self.send_response(200)


def run_server():
    server = HTTPServer(("localhost", LOCAL_PORT), MyRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    server.server_close()


# Start a server on 8888 and expose a callback endpoint
# the tunnel address will be different


def get_auth_token(auth_data):
    global INIT_CALLED, TOKEN_AUTH
    INIT_CALLED += 1
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
    TOKEN_AUTH = token_response.id_token
    return dict(access_token=str(token_response.id_token), expires_in=5 * 60 + 5)


def refresh_auth_token(auth_data):
    global REFRESH_CALLED
    REFRESH_CALLED += 1
    return dict(access_token=TOKEN_AUTH, expires_in=10 * 60)


thread = threading.Thread(target=run_server, daemon=True)
thread.start()

# print(get_auth_token(auth_data))

# Test token expiration and refresh
props = dict(on_oidc_request_token=get_auth_token)
client = MongoClient(port=8889, authmechanismproperties=props, authmechanism="MONGODB-OIDC")
print(client.test.command("ping"))
assert INIT_CALLED == 1
time.sleep(6)
client2 = MongoClient(port=8889, authmechanismproperties=props, authmechanism="MONGODB-OIDC")
print(client2.test.command("ping"))
assert INIT_CALLED == 1
assert REFRESH_CALLED == 1
time.sleep(6)
client3 = MongoClient(port=8889, authmechanismproperties=props, authmechanism="MONGODB-OIDC")
print(client3.test.command("ping"))
assert INIT_CALLED == 1
assert REFRESH_CALLED == 1
