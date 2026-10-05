"""A real storage contract without access to the operating system keyring."""

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    """Store entries by service/account; preserve missing-entry semantics."""

    priority = 1

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[service, username] = password

    def delete_password(self, service, username):
        try:
            del self.values[service, username]
        except KeyError:
            raise PasswordDeleteError("No in-memory entry") from None
