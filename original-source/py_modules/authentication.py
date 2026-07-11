import logging
import importlib, sys
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
#    try:
#        sys.path.remove(str(parent))
#    except ValueError: # already removed
#        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()
from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME
from py_modules.logging_lib import setup_logging, MyLogger
if __name__ == '__main__':
    check_conda_interpreter(CONDA_ENV_NAME)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import typing
import cryptography.hazmat.primitives.asymmetric.ed25519 as ed25519_key
import cryptography.hazmat.primitives.asymmetric.rsa as rsa_key
import cryptography.hazmat.primitives.asymmetric.dsa as dsa_key
import cryptography.hazmat.primitives.asymmetric.ec as ec_key

# _SSH_PUBLIC_KEY_TYPES = typing.Union[
#     ec_key.EllipticCurvePublicKey,
#     rsa_key.RSAPublicKey,
#     dsa_key.DSAPublicKey,
#     ed25519_key.Ed25519PublicKey,
# ]
_SSH_PUBLIC_KEY_TYPES = ed25519_key.Ed25519PublicKey
# _SSH_PRIVATE_KEY_TYPES = typing.Union[
#     ec_key.EllipticCurvePrivateKey,
#     rsa_key.RSAPrivateKey,
#     dsa_key.DSAPrivateKey,
#     ed25519_key.Ed25519PrivateKey,
# ]
_SSH_PRIVATE_KEY_TYPES = ed25519_key.Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
import base64


handshake_info = "hello".encode("ascii")

class AuthenticationServer:
    def __init__(self, authorized_keys_filename: str, logger: MyLogger) -> None:
        self.logger = logger
        self.public_keys: list[_SSH_PUBLIC_KEY_TYPES] = []
        self.load_authorized_keys(authorized_keys_filename)

    def load_authorized_keys(self, filename: str) -> None:
        with open(filename, "rb") as f:
            for line in f.readlines():
                line = line.strip()
                elements = line.split()
                if len(elements) == 3 and elements[0] == b"ssh-ed25519":
                    self.logger.debug(f"Loading public key: {line}")
                    public_key = load_ssh_public_key(line, default_backend())
                    self.logger.debug(f"Loaded public key: {public_key}")
                    assert isinstance(public_key, _SSH_PUBLIC_KEY_TYPES)
                    self.public_keys.append(public_key)

    def validate_request(self, request: bytes, signature_b64: bytes) -> bool:
        # Decode the Base64 encoded signature
        signature = base64.urlsafe_b64decode(signature_b64)
        # Verify the signature using the public keys
        for public_key in self.public_keys:
            try:
                public_key.verify(signature, request)
                self.logger.debug(f"Validated request: {request} with signature: {signature_b64} using key {public_key}")
                return True
            except Exception as e:
                self.logger.debug(f"Failed to validate request: {request} with signature: {signature_b64} using key {public_key}")
                pass
        return False


class AuthenticationClient:
    def __init__(self, private_key_filename: str, logger: MyLogger) -> None:
        self.logger = logger
        self.private_key = self.load_private_key(private_key_filename)

    def load_private_key(self, filename: str) -> _SSH_PRIVATE_KEY_TYPES:
        private_key = serialization.load_ssh_private_key(open(filename, "rb").read(), password=None,
                                                         backend=default_backend())
        assert isinstance(private_key, _SSH_PRIVATE_KEY_TYPES)
        return private_key

    def sign_request(self, request: bytes) -> str:
        signature = self.private_key.sign(request)
        signature_b64 = base64.urlsafe_b64encode(signature)
        # self.logger.debug(f"Signed request: {request} with signature:{signature_b64}")
        return signature_b64.decode("ascii")

import os, sys
if __name__ == "__main__":
    logger = setup_logging()
    os.chdir(os.path.expanduser("~/.ssh"))
    server = AuthenticationServer("authorized_keys", logger)
    client = AuthenticationClient("id_ed25519_android", logger)
    sig = client.sign_request(handshake_info)
    server.validate_request(handshake_info, sig.encode("ascii"))

