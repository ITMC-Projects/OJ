import re

import bcrypt
from django.contrib.auth.hashers import BasePasswordHasher


# Bound imported work factors to avoid an untrusted hash exhausting login workers.
BCRYPT_PATTERN = re.compile(r'\$2[aby]\$(0[4-9]|1[0-6])\$[./A-Za-z0-9]{53}', re.ASCII)


def valid_source_hash(value):
    if not isinstance(value, str) or not BCRYPT_PATTERN.fullmatch(value):
        return False
    # bcrypt rejects invalid salt padding without computing the expensive hash.
    return value[28] in '.Oeu' and value[-1] in '.CGKOSWaeimquy26'


class OTSBcryptPasswordHasher(BasePasswordHasher):
    algorithm = 'ots_bcrypt'

    def encode(self, password, salt):
        raise ValueError('OTS hashes may only be imported from OTS')

    def verify(self, password, encoded):
        raw = encoded.removeprefix(self.algorithm + '$')
        if not valid_source_hash(raw):
            return False
        try:
            # Go rejects passwords longer than bcrypt's 72-byte limit.
            data = password.encode('utf-8')
            return len(data) <= 72 and bcrypt.checkpw(data, raw.encode('ascii'))
        except (ValueError, TypeError, UnicodeError):
            return False

    def safe_summary(self, encoded):
        return {'algorithm': self.algorithm, 'hash': '********'}

    def must_update(self, encoded):
        return False
