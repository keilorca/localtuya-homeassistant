# PyTuya Module
# -*- coding: utf-8 -*-
"""
Python module to interface with Tuya WiFi smart devices.

Mostly derived from Shenzhen Xenon ESP8266MOD WiFi smart devices
E.g. https://wikidevi.com/wiki/Xenon_SM-PW701U

Author: clach04
Maintained by: rospogrigio

For more information see https://github.com/clach04/python-tuya

Classes
   TuyaInterface(dev_id, address, local_key=None)
       dev_id (str): Device ID e.g. 01234567891234567890
       address (str): Device Network IP Address e.g. 10.0.1.99
       local_key (str, optional): The encryption key. Defaults to None.

Functions
   json = status()          # returns json payload
   set_version(version)     #  3.1 [default] or 3.3
   detect_available_dps()   # returns a list of available dps provided by the device
   add_dps_to_request(dps_index)  # adds dps_index to the list of dps used by the
                                  # device (to be queried in the payload)
   set_dps(on, dps_index)   # Set value of any dps index.
   set_timer(num_secs):


Credits
 * TuyaAPI https://github.com/codetheweb/tuyapi by codetheweb and blackrozes
   For protocol reverse engineering
 * PyTuya https://github.com/clach04/python-tuya by clach04
   The origin of this python module (now abandoned)
 * LocalTuya https://github.com/rospogrigio/localtuya-homeassistant by rospogrigio
   Updated pytuya to support devices with Device IDs of 22 characters
"""

import asyncio
import base64
from hashlib import md5
import json
import logging
import time
import binascii
import struct
from collections import namedtuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

version_tuple = (8, 1, 0)
version = version_string = __version__ = "%d.%d.%d" % version_tuple
__author__ = "rospogrigio"

_LOGGER = logging.getLogger(__name__)

TuyaMessage = namedtuple("TuyaMessage", "seqno cmd retcode payload crc")

SET = "set"
STATUS = "status"
HEARTBEAT = "heartbeat"

PROTOCOL_VERSION_BYTES_31 = b"3.1"
PROTOCOL_VERSION_BYTES_33 = b"3.3"

PROTOCOL_33_HEADER = PROTOCOL_VERSION_BYTES_33 + 12 * b"\x00"

MESSAGE_HEADER_FMT = ">4I"  # 4*uint32: prefix, seqno, cmd, length
MESSAGE_RECV_HEADER_FMT = ">5I"  # 4*uint32: prefix, seqno, cmd, length, retcode
MESSAGE_END_FMT = ">2I"  # 2*uint32: crc, suffix

PREFIX_VALUE = 0x000055AA
SUFFIX_VALUE = 0x0000AA55


# This is intended to match requests.json payload at
# https://github.com/codetheweb/tuyapi :
# type_0a devices require the 0a command as the status request
# type_0d devices require the 0d command as the status request, and the list of
# dps used set to null in the request payload (see generate_payload method)

# prefix: # Next byte is command byte ("hexByte") some zero padding, then length
# of remaining payload, i.e. command + suffix (unclear if multiple bytes used for
# length, zero padding implies could be more than one byte)
PAYLOAD_DICT = {
    "type_0a": {
        STATUS: {"hexByte": 0x0A, "command": {"gwId": "", "devId": ""}},
        SET: {"hexByte": 0x07, "command": {"devId": "", "uid": "", "t": ""}},
        HEARTBEAT: {"hexByte": 0x09, "command": {}},
    },
    "type_0d": {
        STATUS: {"hexByte": 0x0D, "command": {"devId": "", "uid": "", "t": ""}},
        SET: {"hexByte": 0x07, "command": {"devId": "", "uid": "", "t": ""}},
        HEARTBEAT: {"hexByte": 0x09, "command": {}},
    },
}


def pack_message(msg):
    """Pack a TuyaMessage into bytes."""
    # Create full message excluding CRC and suffix
    buffer = (
        struct.pack(
            MESSAGE_HEADER_FMT,
            PREFIX_VALUE,
            msg.seqno,
            msg.cmd,
            len(msg.payload) + struct.calcsize(MESSAGE_END_FMT),
        )
        + msg.payload
    )

    # Calculate CRC, add it together with suffix
    buffer += struct.pack(MESSAGE_END_FMT, binascii.crc32(buffer), SUFFIX_VALUE)

    return buffer


def unpack_message(data):
    """Unpack bytes into a TuyaMessage."""
    header_len = struct.calcsize(MESSAGE_RECV_HEADER_FMT)
    end_len = struct.calcsize(MESSAGE_END_FMT)

    _, seqno, cmd, _, retcode = struct.unpack(
        MESSAGE_RECV_HEADER_FMT, data[:header_len]
    )
    payload = data[header_len:-end_len]
    crc, _ = struct.unpack(MESSAGE_END_FMT, data[-end_len:])
    return TuyaMessage(seqno, cmd, retcode, payload, crc)


class AESCipher:
    """Cipher module for Tuya communication."""

    def __init__(self, key):
        """Initialize a new AESCipher."""
        self.bs = 16
        self.cipher = Cipher(algorithms.AES(key), modes.ECB(), default_backend())

    def encrypt(self, raw, use_base64=True):
        """Encrypt data to be sent to device."""
        encryptor = self.cipher.encryptor()
        crypted_text = encryptor.update(self._pad(raw)) + encryptor.finalize()
        return base64.b64encode(crypted_text) if use_base64 else crypted_text

    def decrypt(self, enc, use_base64=True):
        """Decrypt data from device."""
        if use_base64:
            enc = base64.b64decode(enc)

        decryptor = self.cipher.decryptor()
        return self._unpad(decryptor.update(enc) + decryptor.finalize()).decode()

    def _pad(self, s):
        padnum = self.bs - len(s) % self.bs
        return s + padnum * chr(padnum).encode()

    @staticmethod
    def _unpad(s):
        return s[: -ord(s[len(s) - 1 :])]


class MessageDispatcher:
    """Buffer and dispatcher for Tuya messages."""

    def __init__(self, listener):
        """Initialize a new MessageBuffer."""
        self.buffer = b""
        self.listeners = {}
        self.last_seqno = -1
        self.listener = listener

    def abort(self):
        """Abort all waiting clients."""
        for key in self.listeners:
            sem = self.listeners[key]
            self.listeners[key] = None
            sem.release()

    async def wait_for(self, seqno, timeout=5):
        """Wait for response to a sequence number to be received and return it."""
        if self.last_seqno >= seqno:
            return None

        if seqno in self.listeners:
            raise Exception(f"listener exists for {seqno}")

        _LOGGER.debug("Waiting for sequence number %d", seqno)
        self.listeners[seqno] = asyncio.Semaphore(0)
        try:
            await asyncio.wait_for(self.listeners[seqno].acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            del self.listeners[seqno]
            raise

        return self.listeners.pop(seqno)

    def add_data(self, data):
        """Add new data to the buffer and try to parse messages."""
        self.buffer += data
        header_len = struct.calcsize(MESSAGE_RECV_HEADER_FMT)

        while self.buffer:
            # Check if enough data for measage header
            if len(self.buffer) < header_len:
                break

            # Parse header and check if enough data according to length in header
            _, seqno, cmd, length, retcode = struct.unpack_from(
                MESSAGE_RECV_HEADER_FMT, self.buffer
            )
            if len(self.buffer[header_len - 4 :]) < length:
                break

            # length includes payload length, retcode, crc and suffix
            payload_length = length - 4 - struct.calcsize(MESSAGE_END_FMT)
            payload = self.buffer[header_len : header_len + payload_length]

            crc, _ = struct.unpack_from(
                MESSAGE_END_FMT,
                self.buffer[header_len + payload_length : header_len + length],
            )

            self.buffer = self.buffer[header_len + length - 4 :]
            self._dispatch(TuyaMessage(seqno, cmd, retcode, payload, crc))

    def _dispatch(self, msg):
        """Dispatch a message to someone that is listening."""
        self.last_seqno = max(self.last_seqno, msg.seqno)
        if msg.seqno in self.listeners:
            _LOGGER.debug("Dispatching sequence number %d", msg.seqno)
            sem = self.listeners[msg.seqno]
            self.listeners[msg.seqno] = msg
            sem.release()
        elif msg.cmd == 0x09:
            _LOGGER.debug("Got heartbeat response")
        elif msg.cmd == 0x08:
            _LOGGER.debug("Got status update")
            self.listener(msg)
        else:
            _LOGGER.debug(
                "Got message type %d for unknown listener %d: %s",
                msg.command,
                msg.seqno,
                msg,
            )


class TuyaProtocol(asyncio.Protocol):
    """Implementation of the Tuya protocol."""

    def __init__(
        self, dev_id, local_key, protocol_version, on_connected, status_listener
    ):
        """
        Initialize a new TuyaInterface.

        Args:
            dev_id (str): The device id.
            address (str): The network address.
            local_key (str, optional): The encryption key. Defaults to None.

        Attributes:
            port (int): The port to connect to.
        """
        self.loop = asyncio.get_running_loop()
        self.id = dev_id
        self.local_key = local_key.encode("latin1")
        self.version = protocol_version
        self.dev_type = "type_0a"
        self.dps_to_request = {}
        self.cipher = AESCipher(self.local_key)
        self.seqno = 0
        self.transport = None
        self.dispatcher = self._setup_dispatcher(status_listener)
        self.on_connected = on_connected
        self.heartbeater = None

    def _setup_dispatcher(self, status_listener):
        def _status_update(msg):
            status_listener(self._decode_payload(msg.payload))

        return MessageDispatcher(_status_update)

    def close(self):
        """Close connection and abort all outstanding listeners."""
        if self.transport is not None:
            self.dispatcher.abort()
            self.heartbeater.cancel()
            transport = self.transport
            self.transport = None
            transport.close()

    async def exchange(self, command, dps=None):
        """Send and receive a message, returning response from device."""
        _LOGGER.debug("Sending command %s (device type: %s)", command, self.dev_type)
        payload = self._generate_payload(command, dps)
        dev_type = self.dev_type

        self.transport.write(payload)
        msg = await self.dispatcher.wait_for(self.seqno - 1)
        if msg is None:
            _LOGGER.debug("Wait was aborted for %d", self.seqno - 1)
            return None

        # TODO: Verify stuff, e.g. CRC sequence number?
        payload = self._decode_payload(msg.payload)

        # Perform a new exchange (once) if we switched device type
        if dev_type != self.dev_type:
            _LOGGER.debug(
                "Re-send %s due to device type change (%s -> %s)",
                command,
                dev_type,
                self.dev_type,
            )
            return self.exchange(command, dps)
        return payload

    async def status(self):
        """Return device status."""
        return await self.exchange(STATUS)

    def heartbeat(self):
        """Send a heartbeat message."""
        # We don't expect a response to this, just send blindly
        self.transport.write(self._generate_payload(HEARTBEAT))

    async def set_dps(self, value, dps_index):
        """
        Set value (may be any type: bool, int or string) of any dps index.

        Args:
            dps_index(int):   dps index to set
            value: new value for the dps index
        """
        return await self.exchange(SET, {str(dps_index): value})

    async def detect_available_dps(self):
        """Return which datapoints are supported by the device."""
        # type_0d devices need a sort of bruteforce querying in order to detect the
        # list of available dps experience shows that the dps available are usually
        # in the ranges [1-25] and [100-110] need to split the bruteforcing in
        # different steps due to request payload limitation (max. length = 255)
        detected_dps = {}
        ranges = [(2, 11), (11, 21), (21, 31), (100, 111)]

        for dps_range in ranges:
            # dps 1 must always be sent, otherwise it might fail in case no dps is found
            # in the requested range
            self.dps_to_request = {"1": None}
            self.add_dps_to_request(range(*dps_range))
            try:
                data = await self.status()
            except Exception as e:
                _LOGGER.warning("Failed to get status: %s", e)
                raise
            detected_dps.update(data["dps"])

            if self.dev_type == "type_0a":
                return detected_dps

        return detected_dps

    def add_dps_to_request(self, dps_index):
        """Add a datapoint (DP) to be included in requests."""
        if isinstance(dps_index, int):
            self.dps_to_request[str(dps_index)] = None
        else:
            self.dps_to_request.update({str(index): None for index in dps_index})

    def _decode_payload(self, payload):
        _LOGGER.debug("decode payload=%r", payload)

        if not payload:
            payload = "{}"
        elif payload.startswith(PROTOCOL_VERSION_BYTES_31):
            payload = payload[len(PROTOCOL_VERSION_BYTES_31) :]  # remove version header
            # remove (what I'm guessing, but not confirmed is) 16-bytes of MD5
            # hexdigest of payload
            payload = self.cipher.decrypt(payload[16:])
        elif self.version == 3.3:
            if self.dev_type != "type_0a" or payload.startswith(
                PROTOCOL_VERSION_BYTES_33
            ):
                payload = payload[len(PROTOCOL_33_HEADER) :]
            payload = self.cipher.decrypt(payload, False)

            if "data unvalid" in payload:
                self.dev_type = "type_0d"
                _LOGGER.debug(
                    "'data unvalid' error detected: switching to dev_type %r",
                    self.dev_type,
                )
                return None
        elif not payload.startswith(b"{"):
            raise Exception(f"Unexpected payload={payload}")

        if not isinstance(payload, str):
            payload = payload.decode()
        _LOGGER.debug("decrypted result=%r", payload)
        return json.loads(payload)

    def _generate_payload(self, command, data=None):
        """
        Generate the payload to send.

        Args:
            command(str): The type of command.
                This is one of the entries from payload_dict
            data(dict, optional): The data to be send.
                This is what will be passed via the 'dps' entry
        """
        cmd_data = PAYLOAD_DICT[self.dev_type][command]
        json_data = cmd_data["command"]
        command_hb = cmd_data["hexByte"]

        if "gwId" in json_data:
            json_data["gwId"] = self.id
        if "devId" in json_data:
            json_data["devId"] = self.id
        if "uid" in json_data:
            json_data["uid"] = self.id  # still use id, no separate uid
        if "t" in json_data:
            json_data["t"] = str(int(time.time()))

        if data is not None:
            json_data["dps"] = data
        if command_hb == 0x0D:
            json_data["dps"] = self.dps_to_request

        payload = json.dumps(json_data).replace(" ", "").encode("utf-8")
        _LOGGER.debug("paylod=%r", payload)

        if self.version == 3.3:
            payload = self.cipher.encrypt(payload, False)
            if command_hb != 0x0A:
                # add the 3.3 header
                payload = PROTOCOL_33_HEADER + payload
        elif command == SET:
            payload = self.cipher.encrypt(payload)
            preMd5String = (
                b"data="
                + payload
                + b"||lpv="
                + PROTOCOL_VERSION_BYTES_31
                + b"||"
                + self.local_key
            )
            m = md5()
            m.update(preMd5String)
            hexdigest = m.hexdigest()
            payload = (
                PROTOCOL_VERSION_BYTES_31
                + hexdigest[8:][:16].encode("latin1")
                + payload
            )

        msg = TuyaMessage(self.seqno, command_hb, 0, payload, 0)
        self.seqno += 1
        return pack_message(msg)

    def __repr__(self):
        """Return internal string representation of object."""
        return self.id

    def connection_made(self, transport):
        """Did connect to the device."""

        async def heartbeat_loop():
            """Continuously send heart beat updates."""
            while True:
                self.heartbeat()
                await asyncio.sleep(20)

        _LOGGER.debug("Connected")
        self.transport = transport
        self.on_connected.set_result(True)
        self.heartbeater = self.loop.create_task(heartbeat_loop())

    def data_received(self, data):
        """Received data from device."""
        _LOGGER.debug("Received data: %s", data)
        self.dispatcher.add_data(data)

    def connection_lost(self, exc):
        """Disconnected from device."""
        self.close()


async def connect(
    address,
    device_id,
    local_key,
    protocol_version,
    status_listener,
    port=6668,
    timeout=5,
):
    """Connect to a device."""
    loop = asyncio.get_running_loop()
    on_connected = loop.create_future()
    _, protocol = await loop.create_connection(
        lambda: TuyaProtocol(
            device_id, local_key, protocol_version, on_connected, status_listener
        ),
        address,
        port,
    )

    await asyncio.wait_for(on_connected, timeout=timeout)
    return protocol
