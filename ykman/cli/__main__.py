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

from yubikit.core import USB_INTERFACE, ApplicationNotAvailableError
from yubikit.core.fido import FidoConnection
from yubikit.core.smartcard import SmartCardConnection

import ykman.logging_setup

from .. import __version__
from ..scard import list_devices as list_ccid, list_readers
from ..util import Cve201715361VulnerableError
from ..device import (
    read_info,
    get_name,
    list_all_devices,
    scan_devices,
    get_connection_types,
    connect_to_device,
)
from .util import UpperCaseChoice, YkmanContextObject
from .info import info
from .mode import mode
from .otp import otp
from .opgp import openpgp
from .oath import oath
from .piv import piv
from .fido import fido
from .config import config
import click
import time
import logging
import sys


logger = logging.getLogger(__name__)


CLICK_CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"], max_content_width=999)


def retrying_connect(serial, interfaces, attempts=10):
    while True:
        try:
            return connect_to_device(serial, get_connection_types(interfaces))
        except Exception as e:
            if attempts:
                attempts -= 1
                logger.error("Failed opening connection, retry in 0.5s", exc_info=e)
                time.sleep(0.5)
            else:
                raise


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo("YubiKey Manager (ykman) version: {}".format(__version__))
    ctx.exit()


def _disabled_interface(ctx, interfaces, cmd_name):
    req = ", ".join((t.name for t in USB_INTERFACE if t & interfaces))
    click.echo(
        "Command '{}' requires one of the following USB interfaces "
        "to be enabled: '{}'.".format(cmd_name, req)
    )
    ctx.fail("Use 'ykman config usb' to set the enabled USB interfaces.")


def _run_cmd_for_serial(ctx, cmd, interfaces, serial):
    try:
        return retrying_connect(serial, interfaces)
    except ValueError:
        try:
            # Serial not found, see if it's among other interfaces in USB enabled:
            conn = retrying_connect(serial, sum(USB_INTERFACE) ^ interfaces)[0]
            conn.close()
            _disabled_interface(ctx, interfaces, cmd)
        except ValueError:
            ctx.fail(
                "Failed connecting to a YubiKey with serial: {}. "
                "Make sure the application has the required "
                "permissions.".format(serial)
            )


def _run_cmd_for_single(ctx, cmd, interfaces, reader_name=None):
    # Use a specific CCID reader
    if reader_name:
        if USB_INTERFACE.CCID in interfaces or cmd in (fido.name, otp.name):
            readers = list_ccid(reader_name)
            if len(readers) == 1:
                dev = readers[0]
                try:
                    if cmd == fido.name:
                        conn = dev.open_connection(FidoConnection)
                    else:
                        conn = dev.open_connection(SmartCardConnection)
                    info = read_info(dev.pid, conn)
                    return conn, dev.pid, info
                except Exception as e:
                    logger.error("Failure connecting to card", exc_info=e)
                    ctx.fail("Failed to connect: {}".format(e))
            elif len(readers) > 1:
                ctx.fail("Multiple YubiKeys on external readers detected.")
            else:
                ctx.fail("No YubiKey found on external reader.")
        else:
            ctx.fail("Not a CCID command.")

    # Find all connected devices
    devices, _ = scan_devices()
    n_devs = sum(devices.values())
    if n_devs == 0:
        ctx.fail("No YubiKey detected!")
    if n_devs > 1:
        ctx.fail(
            "Multiple YubiKeys detected. Use --device SERIAL to specify "
            "which one to use."
        )

    # Only one connected device, check if any needed interfaces are available
    pid = next(iter(devices.keys()))
    if pid.get_interfaces() & interfaces:
        return retrying_connect(None, interfaces)
    _disabled_interface(ctx, interfaces, cmd)


@click.group(context_settings=CLICK_CONTEXT_SETTINGS)
@click.option(
    "-v",
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
)
@click.option("-d", "--device", type=int, metavar="SERIAL")
@click.option(
    "-l",
    "--log-level",
    default=None,
    type=UpperCaseChoice(ykman.logging_setup.LOG_LEVEL_NAMES),
    help="Enable logging at given verbosity level.",
)
@click.option(
    "--log-file",
    default=None,
    type=str,
    metavar="FILE",
    help="Write logs to the given FILE instead of standard error; "
    "ignored unless --log-level is also set.",
)
@click.option(
    "-r",
    "--reader",
    help="Use an external smart card reader. Conflicts with --device and " "list.",
    metavar="NAME",
    default=None,
)
@click.pass_context
def cli(ctx, device, log_level, log_file, reader):
    """
    Configure your YubiKey via the command line.

    Examples:

    \b
      List connected YubiKeys, only output serial number:
      $ ykman list --serials

    \b
      Show information about YubiKey with serial number 0123456:
      $ ykman --device 0123456 info
    """
    ctx.obj = YkmanContextObject()

    if log_level:
        ykman.logging_setup.setup(log_level, log_file=log_file)

    if reader and device:
        ctx.fail("--reader and --device options can't be combined.")

    subcmd = next(c for c in COMMANDS if c.name == ctx.invoked_subcommand)
    if subcmd == list_keys:
        if reader:
            ctx.fail("--reader and list command can't be combined.")
        return

    interfaces = getattr(subcmd, "interfaces", USB_INTERFACE(sum(USB_INTERFACE)))
    if interfaces:

        def resolve():
            if not getattr(resolve, "items", None):
                if device is not None:
                    resolve.items = _run_cmd_for_serial(
                        ctx, subcmd.name, interfaces, device
                    )
                else:
                    resolve.items = _run_cmd_for_single(
                        ctx, subcmd.name, interfaces, reader
                    )
                ctx.call_on_close(resolve.items[0].close)
            return resolve.items

        ctx.obj.add_resolver("conn", lambda: resolve()[0])
        ctx.obj.add_resolver("pid", lambda: resolve()[1])
        ctx.obj.add_resolver("info", lambda: resolve()[2])


@cli.command("list")
@click.option(
    "-s",
    "--serials",
    is_flag=True,
    help="Output only serial "
    "numbers, one per line (devices without serial will be omitted).",
)
@click.option(
    "-r", "--readers", is_flag=True, help="List available smart card readers."
)
@click.pass_context
def list_keys(ctx, serials, readers):
    """
    List connected YubiKeys.
    """

    if readers:
        for reader in list_readers():
            click.echo(reader.name)
        ctx.exit()

    # List all attached devices
    for pid, dev_info in list_all_devices():
        if serials:
            if dev_info.serial:
                click.echo(dev_info.serial)
        else:
            click.echo(
                "{} ({}) [{}]{}".format(
                    get_name(dev_info, pid.get_type()),
                    "%d.%d.%d" % dev_info.version if dev_info.version else "unknown",
                    pid.name.split("_", 1)[1].replace("_", "+"),
                    " Serial: {}".format(dev_info.serial) if dev_info.serial else "",
                )
            )


COMMANDS = (list_keys, info, mode, otp, openpgp, oath, piv, fido, config)


for cmd in COMMANDS:
    cli.add_command(cmd)


def main():
    try:
        cli(obj={})
    except ApplicationNotAvailableError as e:
        logger.error("Error", exc_info=e)
        click.echo(
            "The functionality required for this command is not enabled or not "
            "available on this YubiKey."
        )
        return 1
    except ValueError as e:
        logger.error("Error", exc_info=e)
        click.echo("Error: " + str(e))
        return 1
    except Cve201715361VulnerableError as err:
        logger.error("Error", exc_info=err)
        click.echo("Error: " + str(err))
        return 2


if __name__ == "__main__":
    sys.exit(main())
