import threading


class AuthException(Exception):
    pass


class Auth:
    def __init__(self, timeout=30):
        self.timeout = timeout
        self.down = False
        self.lock = threading.Lock()
        self.pending_tokens = {}  # token -> event
        self.confirmed_tokens = {}  # token -> openid

    def shutdown(self):
        """Stop all pending requests.
        After shutdown, further actions on this object will raise."""
        with self.lock:
            self.down = True
            for event in self.pending_tokens.values():
                event.set()

    def request_login(self, token):
        """Add a new login token. Blocks until confirmed or timeout.
        Returns True if confirmation is successful.
        Raise if the token is invalid.
        """
        if self.down:
            raise AuthException

        with self.lock:
            if token in self.pending_tokens or token in self.confirmed_tokens:
                raise AuthException
            event = threading.Event()
            self.pending_tokens[token] = event

        result = event.wait(timeout=self.timeout)
        with self.lock:
            del self.pending_tokens[token]
        if self.down:
            return False
        else:
            return result

    def confirm_login(self, token, openid):
        """Confirm a token and associate it with an openid.
        Raise if the token does not exist.
        """
        if self.down:
            raise AuthException

        with self.lock:
            if token not in self.pending_tokens:
                raise AuthException
            self.pending_tokens[token].set()
            self.confirmed_tokens[token] = openid

    def get_openid(self, token):
        """Returns openid corresponding to a confirmed token.
        Raise if the token does not exist.
        """
        if self.down:
            raise AuthException

        if token not in self.confirmed_tokens:
            raise AuthException
        return self.confirmed_tokens[token]
