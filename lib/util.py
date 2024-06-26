# Copyright (c) 2016-2017, Neil Booth
#
# All rights reserved.
#
# The MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# and warranty status of this software.

'''Miscellaneous utility classes and functions.'''


import array
import inspect
from ipaddress import ip_address
import logging
import re
import sys
from collections.abc import Container, Mapping
from struct import pack, Struct


class LoggedClass(object):

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        self.log_prefix = ''
        self.throttled = 0

    def log_info(self, msg, throttle=False):
        # Prevent annoying log messages by throttling them if there
        # are too many in a short period
        if throttle:
            self.throttled += 1
            if self.throttled > 3:
                return
            if self.throttled == 3:
                msg += ' (throttling later logs)'
        self.logger.info(self.log_prefix + msg)

    def log_warning(self, msg):
        self.logger.warning(self.log_prefix + msg)

    def log_error(self, msg):
        self.logger.error(self.log_prefix + msg)


# Method decorator.  To be used for calculations that will always
# deliver the same result.  The method cannot take any arguments
# and should be accessed as an attribute.
class cachedproperty(object):

    def __init__(self, f):
        self.f = f

    def __get__(self, obj, type):
        obj = obj or type
        value = self.f(obj)
        setattr(obj, self.f.__name__, value)
        return value


def formatted_time(t, sep=' '):
    '''Return a number of seconds as a string in days, hours, mins and
    maybe secs.'''
    t = int(t)
    fmts = (('{:d}d', 86400), ('{:02d}h', 3600), ('{:02d}m', 60))
    parts = []
    for fmt, n in fmts:
        val = t // n
        if parts or val:
            parts.append(fmt.format(val))
        t %= n
    if len(parts) < 3:
        parts.append('{:02d}s'.format(t))
    return sep.join(parts)


def deep_getsizeof(obj):
    """Find the memory footprint of a Python object.

    Based on code from code.tutsplus.com: http://goo.gl/fZ0DXK

    This is a recursive function that drills down a Python object graph
    like a dictionary holding nested dictionaries with lists of lists
    and tuples and sets.

    The sys.getsizeof function does a shallow size of only. It counts each
    object inside a container as pointer only regardless of how big it
    really is.
    """

    ids = set()

    def size(o):
        if id(o) in ids:
            return 0

        r = sys.getsizeof(o)
        ids.add(id(o))

        if isinstance(o, (str, bytes, bytearray, array.array)):
            return r

        if isinstance(o, Mapping):
            return r + sum(size(k) + size(v) for k, v in o.items())

        if isinstance(o, Container):
            return r + sum(size(x) for x in o)

        return r

    return size(obj)


def subclasses(base_class, strict=True):
    '''Return a list of subclasses of base_class in its module.'''
    def select(obj):
        return (inspect.isclass(obj) and issubclass(obj, base_class) and
                (not strict or obj != base_class))

    pairs = inspect.getmembers(sys.modules[base_class.__module__], select)
    return [pair[1] for pair in pairs]


def chunks(items, size):
    '''Break up items, an iterable, into chunks of length size.'''
    for i in range(0, len(items), size):
        yield items[i: i + size]


def bytes_to_int(be_bytes):
    '''Interprets a big-endian sequence of bytes as an integer'''
    return int.from_bytes(be_bytes, 'big')


def int_to_bytes(value):
    '''Converts an integer to a big-endian sequence of bytes'''
    return value.to_bytes((value.bit_length() + 7) // 8, 'big')


def int_to_varint(value):
    '''Converts an integer to a Bitcoin-like varint bytes'''
    if value < 0:
        raise ValueError("attempt to write size < 0")
    elif value < 253:
        return pack('<B', value)
    elif value < 2**16:
        return b'\xfd' + pack('<H', value)
    elif value < 2**32:
        return b'\xfe' + pack('<I', value)
    elif value < 2**64:
        return b'\xff' + pack('<Q', value)


def increment_byte_string(bs):
    '''Return the lexicographically next byte string of the same length.

    Return None if there is none (when the input is all 0xff bytes).'''
    for n in range(1, len(bs) + 1):
        if bs[-n] != 0xff:
            return bs[:-n] + bytes([bs[-n] + 1]) + bytes(n - 1)
    return None


class LogicalFile(object):
    '''A logical binary file split across several separate files on disk.'''

    def __init__(self, prefix, digits, file_size):
        digit_fmt = '{' + ':0{:d}d'.format(digits) + '}'
        self.filename_fmt = prefix + digit_fmt
        self.file_size = file_size

    def read(self, start, size=-1):
        '''Read up to size bytes from the virtual file, starting at offset
        start, and return them.

        If size is -1 all bytes are read.'''
        parts = []
        while size != 0:
            try:
                with self.open_file(start, False) as f:
                    part = f.read(size)
                if not part:
                    break
            except FileNotFoundError:
                break
            parts.append(part)
            start += len(part)
            if size > 0:
                size -= len(part)
        return b''.join(parts)

    def write(self, start, b):
        '''Write the bytes-like object, b, to the underlying virtual file.'''
        while b:
            size = min(len(b), self.file_size - (start % self.file_size))
            with self.open_file(start, True) as f:
                f.write(b if size == len(b) else b[:size])
            b = b[size:]
            start += size

    def open_file(self, start, create):
        '''Open the virtual file and seek to start.  Return a file handle.
        Raise FileNotFoundError if the file does not exist and create
        is False.
        '''
        file_num, offset = divmod(start, self.file_size)
        filename = self.filename_fmt.format(file_num)
        f = open_file(filename, create)
        f.seek(offset)
        return f


def open_file(filename, create=False):
    '''Open the file name.  Return its handle.'''
    try:
        return open(filename, 'rb+')
    except FileNotFoundError:
        if create:
            return open(filename, 'wb+')
        raise


def open_truncate(filename):
    '''Open the file name.  Return its handle.'''
    return open(filename, 'wb+')


def address_string(address):
    '''Return an address as a correctly formatted string.'''
    fmt = '{}:{:d}'
    host, port = address
    try:
        host = ip_address(host)
    except ValueError:
        pass
    else:
        if host.version == 6:
            fmt = '[{}]:{:d}'
    return fmt.format(host, port)

# See http://stackoverflow.com/questions/2532053/validate-a-hostname-string
# Note underscores are valid in domain names, but strictly invalid in host
# names.  We ignore that distinction.
SEGMENT_REGEX = re.compile("(?!-)[A-Z_\d-]{1,63}(?<!-)$", re.IGNORECASE)
def is_valid_hostname(hostname):
    if len(hostname) > 255:
        return False
    # strip exactly one dot from the right, if present
    if hostname and hostname[-1] == ".":
        hostname = hostname[:-1]
    return all(SEGMENT_REGEX.match(x) for x in hostname.split("."))

def protocol_tuple(s):
    '''Converts a protocol version number, such as "1.0" to a tuple (1, 0).

    If the version number is bad, (0, ) indicating version 0 is returned.'''
    try:
        return tuple(int(part) for part in s.split('.'))
    except Exception:
        return (0, )

def protocol_version_string(ptuple):
    '''Convert a version tuple such as (1, 2) to "1.2".
    There is always at least one dot, so (1, ) becomes "1.0".'''
    while len(ptuple) < 2:
        ptuple += (0, )
    return '.'.join(str(p) for p in ptuple)

def protocol_version(client_req, server_min, server_max):
    '''Given a client protocol request, return the protocol version
    to use as a tuple.

    If a mutually acceptable protocol version does not exist, return None.
    '''
    if isinstance(client_req, list) and len(client_req) == 2:
        client_min, client_max = client_req
    elif client_req is None:
        client_min = client_max = server_min
    else:
        client_min = client_max = client_req

    client_min = protocol_tuple(client_min)
    client_max = protocol_tuple(client_max)
    server_min = protocol_tuple(server_min)
    server_max = protocol_tuple(server_max)

    result = min(client_max, server_max)
    if result < max(client_min, server_min) or result == (0, ):
        result = None

    return result

unpack_int32_from = Struct('<i').unpack_from
unpack_int64_from = Struct('<q').unpack_from
unpack_uint16_from = Struct('<H').unpack_from
unpack_uint32_from = Struct('<I').unpack_from
unpack_uint64_from = Struct('<Q').unpack_from

hex_to_bytes = bytes.fromhex
