# Copyright (c) 2015 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


import os
import hashlib
import struct
import time
import hmac
from enum import IntEnum
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from ykman.yubicommon.compat import byte2int, int2byte
from .driver_ccid import APDUError, OATH_AID, SW_OK
from .util import tlv, parse_tlv


class TAG(IntEnum):
    NAME = 0x71
    NAME_LIST = 0x72
    KEY = 0x73
    CHALLENGE = 0x74
    RESPONSE = 0x75
    TRUNCATED_RESPONSE = 0x76
    HOTP = 0x77
    PROPERTY = 0x78
    IMF = 0x7a
    TOUCH = 0x7c


class ALGO(IntEnum):
    SHA1 = 0x01
    SHA256 = 0x02


class OATH_TYPE(IntEnum):
    HOTP = 0x10
    TOTP = 0x20


class PROPERTIES(IntEnum):
    REQUIRE_TOUCH = 0x02


class INS(IntEnum):
    PUT = 0x01
    DELETE = 0x02
    SET_CODE = 0x03
    RESET = 0x04
    LIST = 0xa1
    CALCULATE = 0xa2
    VALIDATE = 0xa3
    SELECT = 0xa4
    CALCULATE_ALL = 0xa4
    SEND_REMAINING = 0xa5


class MASK(IntEnum):
    ALGO = 0x0f
    TYPE = 0xf0


class SW(IntEnum):
    NO_SPACE = 0x6a84
    COMMAND_ABORTED = 0x6f00
    MORE_DATA = 0x61


class Credential(object):

    def __init__(self, name, code=None, oath_type='', touch=False, algo=None):
        self.name = name
        self.code = code
        self.oath_type = oath_type
        self.touch = touch
        self.algo = algo
        self.hidden = name.startswith('_hidden:')


class OathController(object):

    def __init__(self, driver):
        self._driver = driver
        self.select()

    @property
    def version(self):
        return self._version

    @property
    def locked(self):
        return self._challenge

    def send_apdu(self, cl, ins, p1, p2, data=b''):
        resp, sw = self._driver.send_apdu(cl, ins, p1, p2, data, check=None)
        while (sw >> 8) == SW.MORE_DATA:
            more, sw = self._driver.send_apdu(
                0, INS.SEND_REMAINING, 0, 0, '', check=None)
            resp += more

        if sw != SW_OK:
            raise APDUError(resp, sw)

        return resp

    def select(self):
        resp = self.send_apdu(0, INS.SELECT, 0x04, 0, OATH_AID)
        tags = parse_tlv(resp)
        self._version = tuple(byte2int(x) for x in tags[0]['value'])
        self._id = tags[1]['value']
        if len(tags) > 2:
            self._challenge = tags[2]['value']
        else:
            self._challenge = None

    def reset(self):
        self.send_apdu(0, INS.RESET, 0xde, 0xad)

    def put(self, key, name, oath_type='totp', digits=6,
            algo='SHA1', counter=0, require_touch=False):

        oath_type = OATH_TYPE[oath_type.upper()].value
        algo = ALGO[algo].value

        key = hmac_shorten_key(key, algo)
        key = int2byte(oath_type | algo) + int2byte(digits) + key

        data = tlv(TAG.NAME, name.encode('utf8')) + tlv(TAG.KEY, key)

        properties = 0

        if require_touch:
            properties |= PROPERTIES.REQUIRE_TOUCH

        if properties:
            data += int2byte(TAG.PROPERTY) + int2byte(properties)

        if counter > 0:
            data += tlv(TAG.IMF, struct.pack('>I', counter))

        self.send_apdu(0, INS.PUT, 0, 0, data)

    def list(self):
        resp = self.send_apdu(0, INS.LIST, 0, 0)
        while resp:
            length = byte2int(resp[1]) - 1
            oath_type = (MASK.TYPE & byte2int(resp[2]))
            oath_type = OATH_TYPE(oath_type).name
            algo = (MASK.ALGO & byte2int(resp[2]))
            algo = ALGO(algo).name
            name = resp[3:3 + length].decode('utf-8')
            cred = Credential(name, oath_type=oath_type, algo=algo)
            yield cred
            resp = resp[3 + length:]

    def calculate(self, cred):
        challenge = time_challenge() \
            if cred.oath_type == 'totp' else b''
        data = tlv(TAG.NAME, cred.name.encode('utf-8')) + tlv(
            TAG.CHALLENGE, challenge)
        resp = self.send_apdu(0, INS.CALCULATE, 0, 0x01, data)
        resp = parse_tlv(resp)[0]['value']
        digits = resp[0]
        code = resp[1:]
        code = parse_truncated(code)
        cred.code = format_code(code, digits)
        return cred

    def delete(self, cred):
        data = tlv(TAG.NAME, cred.name.encode('utf-8'))
        self.send_apdu(0, INS.DELETE, 0, 0, data)

    def calculate_all(self):
        data = tlv(TAG.CHALLENGE, time_challenge())
        resp = self.send_apdu(0, INS.CALCULATE_ALL, 0, 0x01, data)
        return _parse_creds(resp)

    def set_password(self, password):
        key = _derive_key(self._id, password)
        keydata = int2byte(OATH_TYPE.TOTP | ALGO.SHA1) + key
        challenge = os.urandom(8)
        response = hmac.new(key, challenge, hashlib.sha1).digest()
        data = tlv(TAG.KEY, keydata) + tlv(TAG.CHALLENGE, challenge) + tlv(
            TAG.RESPONSE, response)
        self.send_apdu(0, INS.SET_CODE, 0, 0, data)

    def clear_password(self):
        self.send_apdu(0, INS.SET_CODE, 0, 0, tlv(TAG.KEY, b''))

    def validate(self, password):
        key = _derive_key(self._id, password)
        response = hmac.new(key, self._challenge, hashlib.sha1).digest()
        challenge = os.urandom(8)
        verification = hmac.new(key, challenge, hashlib.sha1).digest()
        data = tlv(TAG.RESPONSE, response) + tlv(TAG.CHALLENGE, challenge)
        resp = self.send_apdu(0, INS.VALIDATE, 0, 0, data)
        if parse_tlv(resp)[0]['value'] != verification:
            raise ValueError(
                'Response from validation does not match verification!')
        self._challenge = None


def _derive_key(salt, passphrase):
    kdf = PBKDF2HMAC(hashes.SHA1(), 16, salt, 1000, default_backend())
    return kdf.derive(passphrase.encode('utf-8'))


def _parse_creds(data):
    tags = parse_tlv(data)
    while tags:
        name_tag = tags[0]
        resp_tag = tags[1]
        name = name_tag['value'].decode('utf-8')
        resp_type = resp_tag['tag']
        digits = resp_tag['value'][0]
        cred = Credential(name)
        if resp_type == TAG.TRUNCATED_RESPONSE:
            code = parse_truncated(resp_tag['value'][1:])
            cred.code = format_code(code, digits)
            cred.oath_type = 'totp'
        elif resp_type == TAG.HOTP:
            cred.oath_type = 'hotp'
        elif resp_type == TAG.TOUCH:
            cred.touch = True
        yield cred
        tags = tags[2:]


def format_code(code, digits=6):
    return ('%%0%dd' % digits) % (code % 10 ** digits)


def parse_truncated(resp):
    return struct.unpack('>I', resp)[0] & 0x7fffffff


def hmac_shorten_key(key, algo):
    if algo == ALGO.SHA1:
        h = hashlib.sha1()
    elif algo == ALGO.SHA256:
        h = hashlib.sha256()
    else:
        raise ValueError('Unsupported algorithm!')
    if len(key) > h.block_size:
        h.update(key)
        key = h.digest()
    return key


def time_challenge(t=None):
    return struct.pack('>q', int((t or time.time())/30))
