# Copyright (c) 2020 Yubico AB
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

from .core import (
    bytes2int,
    int2bytes,
    Version,
    Tlv,
    AID,
    PID,
    TRANSPORT,
    APPLICATION,
    FORM_FACTOR,
    USB_INTERFACE,
    NotSupportedError,
    BadResponseError,
)
from .core.otp import check_crc, OtpConnection, OtpProtocol
from .core.fido import FidoConnection
from .core.smartcard import SmartCardConnection, SmartCardProtocol

from enum import IntEnum, IntFlag, unique
from dataclasses import dataclass
from typing import Optional, Union, Mapping
import abc
import struct


SLOT_DEVICE_CONFIG = 0x11
SLOT_YK4_CAPABILITIES = 0x13
SLOT_YK4_SET_DEVICE_INFO = 0x15

INS_READ_CONFIG = 0x1D
INS_WRITE_CONFIG = 0x1C
INS_SET_MODE = 0x16
P1_DEVICE_CONFIG = 0x11

CTAP_VENDOR_FIRST = 0x40
CTAP_YUBIKEY_DEVICE_CONFIG = CTAP_VENDOR_FIRST
CTAP_READ_CONFIG = CTAP_VENDOR_FIRST + 2
CTAP_WRITE_CONFIG = CTAP_VENDOR_FIRST + 3


@unique
class DEVICE_FLAG(IntFlag):
    REMOTE_WAKEUP = 0x40
    EJECT = 0x80


class _Backend(abc.ABC):
    version: Version

    @abc.abstractmethod
    def close(self) -> None:
        ...

    @abc.abstractmethod
    def set_mode(self, data: bytes) -> None:
        ...

    @abc.abstractmethod
    def read_config(self) -> bytes:
        ...

    @abc.abstractmethod
    def write_config(self, config: bytes) -> None:
        ...


class _ManagementOtpBackend(_Backend):
    def __init__(self, otp_connection):
        self.protocol = OtpProtocol(otp_connection)
        self.version = Version.from_bytes(self.protocol.read_status()[:3])

    def close(self):
        self.protocol.close()

    def set_mode(self, data):
        self.protocol.send_and_receive(SLOT_DEVICE_CONFIG, data)

    def read_config(self):
        response = self.protocol.send_and_receive(SLOT_YK4_CAPABILITIES)
        r_len = response[0]
        if check_crc(response[: r_len + 1 + 2]):
            return response[: r_len + 1]
        raise BadResponseError("Invalid checksum")

    def write_config(self, config):
        self.protocol.send_and_receive(SLOT_YK4_SET_DEVICE_INFO, config)


class _ManagementSmartCardBackend(_Backend):
    def __init__(self, smartcard_connection):
        self.protocol = SmartCardProtocol(smartcard_connection)
        select_str = self.protocol.select(AID.MGMT).decode()
        self.version = Version.from_string(select_str)

    def close(self):
        self.protocol.close()

    def set_mode(self, data):
        if self.version[0] == 3:
            # Use the OTP Application to set mode
            self.protocol.select(AID.OTP)
            self.protocol.send_apdu(0, 0x01, SLOT_DEVICE_CONFIG, 0, data)
            # Workaround to "de-select" on NEO
            self.protocol.connection.send_and_receive(b"\xa4\x04\x00\x08")
            self.protocol.select(AID.MGMT)
        else:
            self.protocol.send_apdu(0, INS_SET_MODE, P1_DEVICE_CONFIG, 0, data)

    def read_config(self):
        return self.protocol.send_apdu(0, INS_READ_CONFIG, 0, 0)

    def write_config(self, config):
        self.protocol.send_apdu(0, INS_WRITE_CONFIG, 0, 0, config)


class _ManagementCtapBackend(_Backend):
    def __init__(self, fido_connection):
        self.ctap = fido_connection
        version = fido_connection.device_version
        if version[0] < 4:  # Prior to YK4 this was not firmware version
            version = (3, 0, 0)  # Guess
        self.version = Version(*version)

    def close(self):
        self.ctap.close()

    def set_mode(self, data):
        self.ctap.call(CTAP_YUBIKEY_DEVICE_CONFIG, data)

    def read_config(self):
        return self.ctap.call(CTAP_READ_CONFIG)

    def write_config(self, config):
        self.ctap.call(CTAP_WRITE_CONFIG, config)


@unique
class TAG(IntEnum):
    USB_SUPPORTED = 0x01
    SERIAL = 0x02
    USB_ENABLED = 0x03
    FORM_FACTOR = 0x04
    VERSION = 0x05
    AUTO_EJECT_TIMEOUT = 0x06
    CHALRESP_TIMEOUT = 0x07
    DEVICE_FLAGS = 0x08
    APP_VERSIONS = 0x09
    CONFIG_LOCK = 0x0A
    UNLOCK = 0x0B
    REBOOT = 0x0C
    NFC_SUPPORTED = 0x0D
    NFC_ENABLED = 0x0E


@dataclass
class DeviceConfig:
    enabled_applications: Mapping[TRANSPORT, APPLICATION]
    auto_eject_timeout: Optional[int]
    challenge_response_timeout: Optional[int]
    device_flags: Optional[DEVICE_FLAG]

    def get_bytes(
        self,
        reboot: bool,
        cur_lock_code: Optional[bytes] = None,
        new_lock_code: Optional[bytes] = None,
    ) -> bytes:
        buf = b""
        if reboot:
            buf += Tlv(TAG.REBOOT)
        if cur_lock_code:
            buf += Tlv(TAG.UNLOCK, cur_lock_code)
        usb_enabled = self.enabled_applications.get(TRANSPORT.USB)
        if usb_enabled is not None:
            buf += Tlv(TAG.USB_ENABLED, int2bytes(usb_enabled, 2))
        nfc_enabled = self.enabled_applications.get(TRANSPORT.NFC)
        if nfc_enabled is not None:
            buf += Tlv(TAG.NFC_ENABLED, int2bytes(nfc_enabled, 2))
        if self.auto_eject_timeout is not None:
            buf += Tlv(TAG.AUTO_EJECT_TIMEOUT, int2bytes(self.auto_eject_timeout, 2))
        if self.challenge_response_timeout is not None:
            buf += Tlv(TAG.CHALRESP_TIMEOUT, int2bytes(self.challenge_response_timeout))
        if self.device_flags is not None:
            buf += Tlv(TAG.DEVICE_FLAGS, int2bytes(self.device_flags))
        if new_lock_code:
            buf += Tlv(TAG.CONFIG_LOCK, new_lock_code)
        if len(buf) > 0xFF:
            raise NotSupportedError("DeviceConfiguration too large")
        return int2bytes(len(buf)) + buf


@dataclass
class DeviceInfo:
    config: DeviceConfig
    serial: Optional[int]
    version: Version
    form_factor: FORM_FACTOR
    supported_applications: Mapping[TRANSPORT, APPLICATION]
    is_locked: bool

    def has_transport(self, transport: TRANSPORT) -> bool:
        return transport in self.supported_applications

    @classmethod
    def parse(cls, encoded: bytes, default_version: Version) -> "DeviceInfo":
        if len(encoded) - 1 != encoded[0]:
            raise BadResponseError("Invalid length")
        data = Tlv.parse_dict(encoded[1:])
        locked = data.get(TAG.CONFIG_LOCK) == b"\1"
        serial = bytes2int(data.get(TAG.SERIAL, b"\0")) or None
        form_factor = FORM_FACTOR.from_code(bytes2int(data.get(TAG.FORM_FACTOR, b"\0")))
        if TAG.VERSION in data:
            version = Version.from_bytes(data[TAG.VERSION])
        else:
            version = default_version
        auto_eject_to = bytes2int(data.get(TAG.AUTO_EJECT_TIMEOUT, b"\0"))
        chal_resp_to = bytes2int(data.get(TAG.CHALRESP_TIMEOUT, b"\0"))
        flags = DEVICE_FLAG(bytes2int(data.get(TAG.DEVICE_FLAGS, b"\0")))

        supported = {}
        enabled = {}

        if version == (4, 2, 4):  # Doesn't report correctly
            supported[TRANSPORT.USB] = APPLICATION(0x3F)
        else:
            supported[TRANSPORT.USB] = APPLICATION(bytes2int(data[TAG.USB_SUPPORTED]))
        if TAG.USB_ENABLED in data:  # From YK 5.0.0
            enabled[TRANSPORT.USB] = APPLICATION(bytes2int(data[TAG.USB_ENABLED]))
        if TAG.NFC_SUPPORTED in data:  # YK with NFC
            supported[TRANSPORT.NFC] = APPLICATION(bytes2int(data[TAG.NFC_SUPPORTED]))
            enabled[TRANSPORT.NFC] = APPLICATION(bytes2int(data[TAG.NFC_ENABLED]))

        return cls(
            DeviceConfig(enabled, auto_eject_to, chal_resp_to, flags),
            serial,
            version,
            form_factor,
            supported,
            locked,
        )


_MODES = [
    USB_INTERFACE.OTP,  # 0x00
    USB_INTERFACE.CCID,  # 0x01
    USB_INTERFACE.OTP | USB_INTERFACE.CCID,  # 0x02
    USB_INTERFACE.FIDO,  # 0x03
    USB_INTERFACE.OTP | USB_INTERFACE.FIDO,  # 0x04
    USB_INTERFACE.FIDO | USB_INTERFACE.CCID,  # 0x05
    USB_INTERFACE.OTP | USB_INTERFACE.FIDO | USB_INTERFACE.CCID,  # 0x06
]


@dataclass(init=False, repr=False)
class Mode:
    code: int
    interfaces: USB_INTERFACE

    def __init__(self, interfaces: USB_INTERFACE):
        try:
            self.code = _MODES.index(interfaces)
            self.interfaces = USB_INTERFACE(interfaces)
        except ValueError:
            raise ValueError("Invalid mode!")

    def __repr__(self):
        return "+".join(t.name for t in USB_INTERFACE if t in self.interfaces)

    @classmethod
    def from_code(cls, code: int) -> "Mode":
        code = code & 0b00000111
        return cls(_MODES[code])

    @classmethod
    def from_pid(cls, pid: PID) -> "Mode":
        return cls(PID(pid).get_interfaces())


class ManagementSession:
    def __init__(
        self, connection: Union[OtpConnection, SmartCardConnection, FidoConnection]
    ):
        if isinstance(connection, OtpConnection):
            self.backend: _Backend = _ManagementOtpBackend(connection)
        elif isinstance(connection, SmartCardConnection):
            self.backend = _ManagementSmartCardBackend(connection)
        elif isinstance(connection, FidoConnection):
            self.backend = _ManagementCtapBackend(connection)
        else:
            raise TypeError("Unsupported connection type")
        if self.version < (3, 0, 0):
            raise NotSupportedError("ManagementSession requires YubiKey 3 or later")

    def close(self) -> None:
        self.backend.close()

    @property
    def version(self) -> Version:
        return self.backend.version

    def read_device_info(self) -> DeviceInfo:
        if self.version < (4, 1, 0):
            raise NotSupportedError("Operation requires YubiKey 4.1 or later")
        return DeviceInfo.parse(self.backend.read_config(), self.version)

    def write_device_config(
        self,
        config: Optional[DeviceConfig] = None,
        reboot: bool = False,
        cur_lock_code: Optional[bytes] = None,
        new_lock_code: Optional[bytes] = None,
    ) -> None:
        if self.version < (5, 0, 0):
            raise NotSupportedError("Operation requires YubiKey 5 or later")

        config = config or DeviceConfig({}, None, None, None)
        self.backend.write_config(
            config.get_bytes(reboot, cur_lock_code, new_lock_code)
        )

    def set_mode(
        self, mode: Mode, chalresp_timeout: int = 0, auto_eject_timeout: int = 0
    ) -> None:
        if self.version < (3, 0, 0):
            raise NotSupportedError("Changing mode requires YubiKey 3 or later")
        if self.version >= (5, 0, 0):
            # Translate into DeviceConfig
            usb_enabled = APPLICATION(0)
            if USB_INTERFACE.OTP in mode.interfaces:
                usb_enabled |= APPLICATION.OTP
            if USB_INTERFACE.CCID in mode.interfaces:
                usb_enabled |= APPLICATION.OATH | APPLICATION.PIV | APPLICATION.OPGP
            if USB_INTERFACE.FIDO in mode.interfaces:
                usb_enabled |= APPLICATION.U2F | APPLICATION.FIDO2
            self.write_device_config(
                DeviceConfig(
                    {TRANSPORT.USB: usb_enabled},
                    auto_eject_timeout,
                    chalresp_timeout,
                    None,
                )
            )
        else:
            self.backend.set_mode(
                struct.pack(">BBH", mode.code, chalresp_timeout, auto_eject_timeout)
            )
