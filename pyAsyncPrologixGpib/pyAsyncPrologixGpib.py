# -*- coding: utf-8 -*-
# ##### BEGIN GPL LICENSE BLOCK #####
#
# Copyright (C) 2020  Patrick Baus
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

import asyncio
from enum import Enum, unique
from itertools import zip_longest
import re   # needed to escape characters in the byte stream

from .ip_connection import AsyncSharedIPConnection

@unique
class DeviceMode(Enum):
    DEVICE = 0
    CONTROLLER = 1

@unique
class EosMode(Enum):
    APPEND_CR_LF = 0
    APPEND_CR    = 1
    APPEND_LF    = 2
    APPEND_NONE  = 3

# The following characters need to be escaped according to:
# http://prologix.biz/downloads/PrologixGpibEthernetManual.pdf
translation_map = {
    b"\r"  : b"\x1B\r",
    b"\n"  : b"\x1B\n",
    b"+"   : b"\x1B+",
    b"\x1B": b"\x1B\x1B",
}

# Generate a regex pattern which maches either of the characters (|).
# Characters like "\n" need to be escaped when used in a regex, so we run re.ecape on
# all characters first.
escape_pattern = re.compile(b"|".join(map(re.escape, translation_map.keys())))

class AsyncPrologixGpib():
    @property
    def _conn(self):
        return self.__conn

    def __init__(self, conn, pad, device_mode, sad=0, timeout=13, send_eoi=1, eos_mode=0):
        self.__conn = conn
        self.__state = {
          'pad'             : pad,
          'sad'             : sad,
          'send_eoi'        : bool(int(send_eoi)),
          'send_eot'        : False,
          'eot_char'        : b"\n",
          'eos_mode'        : EosMode(eos_mode),
          'timeout'         : timeout,
          'read_after_write': False,
          'device_mode'     : device_mode,
        }

    async def connect(self):
        """
        Connect to the ethernet controller and configure the device as a GPIB controller. By default the configuration
        will not be saved to EEPROM to safe write cycles.
        """
        await self.__conn.connect()
        lock = self.__conn.meta.setdefault("lock", asyncio.Lock())
        async with lock:
            await asyncio.gather(
                self.set_save_config(False),    # Disable saving the config to EEPROM by default to save EEPROM writes
                self.__ensure_state(),
            )

    async def close(self):
        """
        This is an alias for disconnect().
        """
        await self.disconnect()

    async def disconnect(self):
        """
        Close the ip connection and flush its buffers.
        """
        await self.__conn.disconnect()

    async def __ensure_state(self):
        jobs = []
        if self.__state['device_mode'] != self.__conn.meta.get('device_mode'):
            jobs.append(self.__set_device_mode(self.__state['device_mode']))
        if self.__state['pad'] != self.__conn.meta.get('pad') or self.__state['sad'] != self.__conn.meta.get('sad'):
            jobs.append(self.__set_address(self.__state['pad'], self.__state['sad']))
        if self.__state['read_after_write'] != self.__conn.meta.get('read_after_write'):
            jobs.append(self.__set_read_after_write(self.__state['read_after_write']))
        if self.__state['send_eoi'] != self.__conn.meta.get('send_eoi'):
            jobs.append(self.__set_eoi(self.__state['send_eoi']))
        if self.__state['send_eot'] != self.__conn.meta.get('send_eot'):
            jobs.append(self.__set_eot(self.__state['send_eot']))
        if self.__state['eot_char'] != self.__conn.meta.get('eot_char'):
            jobs.append(self.__set_eot_char(self.__state['eot_char']))
        if self.__state['eos_mode'] != self.__conn.meta.get('eos_mode'):
            jobs.append(self.__set_eos_mode(self.__state['eos_mode']))
        if self.__state['timeout'] != self.__conn.meta.get('timeout'):
            jobs.append(self.__timeout(self.__state['timeout']))

        if len(jobs):
            await asyncio.gather(*jobs)

    async def __query_command(self, command):
        """
        Issue a Prologix command and return the result. This function will strip the \r\n control sequence returned
        by the controller.
        """
        await self.__write(command)
        return (await self.__conn.read(eol_character=b"\n"))[:-2]    # strip the EOT characters (\r\n)

    def __escape_data(self, data):
        """
        The prologix adapter uses ++ to signal commands. the \ŗ \n characters are used to separate messages. In order to
        transmit these characters to the GPIB device, they need to be escaped using the ESC character (\x1B). Therefore
        \r, \n, \x1B (27, ESC) and "+" need to be escaped.
        """
        # Use a regex to match them replace them using a translation map
        return escape_pattern.sub(lambda match: translation_map[match.group(0)], data)

    async def __write(self, data):
        await self.__conn.write(data + b"\n")   # Append Prologix termination character

    async def write(self, data):
        """
        Send a byte string to the GPIB device. The byte string will automatically be escaped, so it is not possible to
        send commands to the GPIB controller. Use the appropriate method instead.
        """
        data = self.__escape_data(data)
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write(data)

    async def read(self, len=None, character=None):
        """
        Read data until an EOI (End of Identify) was received (default), or if the character parameter is set until the
        character is received. Using the len parameter it is possible to read only a certain number of bytes.
        The underlying network protocol is a stream protocol, which does not know about the EOI of the GPIB bus. By
        default a packet is terminated by a \n. If the device does not terminate its packets with either \r\n or \n,
        consider using an EOT characer.
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            if character is None:
                await self.__write(b"++read eoi")
            else:
                await self.__write("++read {value:d}".format(value=ord(character)).encode('ascii'))

            return await self.__conn.read(length=len, eol_character=self.__state['eot_char'] if self.__state['send_eot'] else None)

    async def get_device_mode(self):
        """
        Returns the current configuration of the GPIB adapter as a DeviceMode enum.
        """
        async with self.__conn.meta["lock"]:
            return DeviceMode(int(await self.__query_command(b"++mode")))

    async def set_read_after_write(self, enable):
        """
        If enabled automatically triggers the instrument to TALK after each command. If set, the instrument is also
        immediately asked to TALK (enable==True) or LISTEN (enable==False).
        """
        async with self.__conn.meta["lock"]:
            await self.__set_read_after_write(enable)

    async def __set_read_after_write(self, enable):
        await self.__write("++auto {value:d}".format(value=enable).encode('ascii'))
        self.__state['read_after_write'] = enable
        self.__conn.meta['read_after_write'] = enable

    async def get_read_after_write(self):
        """
        Returns True if the instruments will be asked to TALK after each command.
        """
        async with self.__conn.meta["lock"]:
            return bool(int(await self.__query_command(b"++auto")))

    async def set_address(self, pad, sad=0):
        """
        Change the current address of the GPIB controller. If set to device mode, this is the address the controller
        is listening to. If set to controller mode, it is the address of the device being to talked to. The parameters
        pad is ist the primarey address and sad the the secondary address. The primary and secondary address (if set)
        must be in the range of [0,...,30]
        """
        async with self.__conn.meta["lock"]:
            await self.__set_address(pad, sad)

    async def __set_address(self, pad, sad=0):
        assert (0<= pad <=30) and (sad==0 or 0x60<= sad <=0x7e)
        if sad == 0:
          address = "++addr {pad:d}".format(pad=pad).encode('ascii')
        else:
          address = "++addr {pad:d} {sad:d}".format(pad=pad, sad=sad).encode('ascii')

        await self.__write(address)
        self.__state['pad'] = pad
        self.__state['sad'] = sad
        self.__conn.meta['pad'] = pad
        self.__conn.meta['sad'] = sad

    async def get_address(self):
        """
        Returns a dict containing the primary and secondary address currently set.
        """
        indices = ["pad", "sad"]
        async with self.__conn.meta["lock"]:
            result = await self.__query_command(b"++addr")

        # The result might by either "pad" or "pad sad"
        # The secondary address is offset by 0x60 (96).
        # See here for the reason: http://www.ni.com/tutorial/2801/en/#:~:text=The%20secondary%20address%20is%20actually,the%20last%20bit%20is%20not
        # We return a dict looking like this {"pad": pad, "sad": 0} or {"pad": pad, "sad": sad}
        # So we first split the string, then create a list of ints
        result = [int(addr) for i, addr in enumerate(result.split(b" "))]

        # Create the dict, zip_longest pads the shorted list with 0
        return dict(zip_longest(indices, result, fillvalue=0))

    async def set_eoi(self, enable):
        """
        Enable or disable setting the EOI (End of Identify) line after each transfer. Some older devices might not want
        or need the EOI line to be signaled after each command. This is enabled by default.
        """
        async with self.__conn.meta["lock"]:
            await self.__set_eoi(enable)

    async def __set_eoi(self, enable):
        await self.__write("++eoi {value:d}".format(value=enable).encode('ascii'))
        self.__state['send_eoi'] = bool(int(enable))
        self.__conn.meta['send_eoi'] = bool(int(enable))

    async def get_eoi(self):
        """
        Returns True if the EOI line is signaled after each transfer.
        """
        async with self.__conn.meta["lock"]:
            return bool(int(await self.__query_command(b"++eoi")))

    async def set_eos_mode(self, mode):
        """
        Some older devices do not listen to the EOI, but instead for \r, \n or \r\n. Enable this setting by choosing
        the appropriate EosMode enum. The GPIB controller will then automatically append the control character when
        sending the EOI signal.
        """
        async with self.__conn.meta["lock"]:
            await self.__set_eos_mode(mode)

    async def __set_eos_mode(self, mode):
        assert isinstance(mode, EosMode)
        await self.__write("++eos {value:d}".format(value=mode.value).encode('ascii'))
        self.__state['eos_mode'] = mode
        self.__conn.meta['eos_mode'] = mode

    async def get_eos_mode(self):
        """
        Returns an EosMode enum stating if a control character like \r, \n or \r\n is appended to each transmission.
        """
        async with self.__conn.meta["lock"]:
            return EosMode(int(await self.__query_command(b"++eos")))

    async def set_eot(self, enable):
        """
        Enable this to append a character if an EOI is triggered. THe character will be appended to the data coming from
        the device. This is useful, if the device itself does not append a character, because the network protocol does
        not signal the EOI. Note: This feature might be problematic when the transfer type is binary.
        """
        async with self.__conn.meta["lock"]:
            await self.__set_eot(enable)

    async def __set_eot(self, enable):
        await self.__write("++eot_enable {value:d}".format(value=enable).encode('ascii'))
        self.__state['send_eot'] = bool(int(enable))
        self.__conn.meta['send_eot'] = bool(int(enable))

    async def get_eot(self):
        """
        Returns true, if the controller appends a user specified character after receiving an EOI.
        """
        return bool(int(await self.__query_command(b"++eot_enable")))

    async def set_eot_char(self, character):
        """
        Append a character to the device output. Most GPIB devices only use 7-bit characters. So it is typically safe
        to use a character in the range of 0x80 to 0xFF.
        Note: This might not be the case for binary transmissions.
        """
        async with self.__conn.meta["lock"]:
            await self.__set_eot_char(character)

    async def __set_eot_char(self, character):
        await self.__write("++eot_char {value:d}".format(value=ord(character)).encode('ascii'))
        self.__state['eot_char'] = character
        self.__conn.meta['eot_char'] = character

    async def get_eot_char(self):
        """
        Returns the character, which will be appended to after receiving an EOI from the device.
        """
        async with self.__conn.meta["lock"]:
            return chr(int(await self.__query_command(b"++eot_char")))

    async def remote_enable(self, enable=True):
        """
        Set the device to remote mode, typically disabling the front panel.
        """
        if bool(enable):
          await self.__write(b"++llo")

    async def timeout(self, value):
        async with self.__conn.meta["lock"]:
            await self.__timeout(value)

    async def __timeout(self, value):
        """
        Set the GPIB timeout in ms for a read. This is not the network timeout, which comes on top of that.
        """
        assert (1 <= value <= 3000)
        await self.__write("++read_tmo_ms {value:d}".format(value=value).encode('ascii'))
        self.__state['timeout'] = value
        self.__conn.meta['timeout'] = value

    async def ibloc(self):
        """
        Set the device to local mode, return control to the front panel.
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write(b"++loc")

    async def get_status(self):
        """
        Returns the status byte, that will be sent to a controller if serial polled by the
        controller.
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            return await self.__query_command(b"++status")

    async def set_status(self, value):
        """
        Returns the status byte, that will be sent to a controller if serial polled by the
        controller.
        """
        assert (0 <= value <= 255)
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write("++status {value:d}".format(value=value).encode('ascii'))

    async def interface_clear(self):
        """
        Assert the Interface Clear (IFC) line and force the instrument to listen.
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write(b"++ifc")

    async def clear(self):
        """
        Send the Selected Device Clear (SDC) event and the device to clear its input and output buffer.
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write(b"++clr")

    async def trigger(self):
        """
        Trigger the selected instrument
        """
        async with self.__conn.meta["lock"]:
            await self.__ensure_state()
            await self.__write(b"++trg")

    async def version(self):
        """
        Return the version string of the Prologix GPIB controller
        """
        async with self.__conn.meta["lock"]:
            # Return a unicode string
            return (await self.__query_command(b"++ver")).decode()

    async def serial_poll(self, pad=0, sad=0):
        """
        Perform a serial poll of the instrument at the given address. If no address is given poll the current instrument.
        """
        assert (0<= pad <=30) and (sad==0 or 0x60<= sad <=0x7e)
        command = b"++spoll"
        if pad != 0:
            command += b" " + bytes(str(int(pad)), 'ascii')
            if sad != 0:
                command += b" " + bytes(str(int(sad)), 'ascii')
            async with self.__conn.meta["lock"]:
                return await self.__query_command(command)
        else:
            async with self.__conn.meta["lock"]:
                await self.__ensure_state()
                return await self.__query_command(command)


    async def test_srq(self):
        """
        Returns True if the service request line is asserted. This can be used by the device to get the attention of the
        controller without constantly polling read().
        """
        async with self.__conn.meta["lock"]:
            return bool(int(await self.__query_command(b"++srq")))

    async def reset(self):
        """
        Reset the controller. It takes about five seconds for the controller to reboot.
        """
        await self.__write(b"++rst")

    async def set_save_config(self, enable):
        """
        Save the the following configuration options to the controller EEPROM:
        DeviceMode, GPIB address, read-after-write, EOI, EOS, EOT, EOT character and the timeout
        Note: this will wear out the EEPROM if used very, very frequently. It is disabled by default.
        """
        await self.__write("++savecfg {value:d}".format(value=enable).encode('ascii'))

    async def get_save_config(self):
        """
        Returns True if the following options are saved to the EEPROM:
        DeviceMode, GPIB address, read-after-write, EOI, EOS, EOT, EOT character and the timeout
        """
        async with self.__conn.meta["lock"]:
            return bool(int(await self.__query_command(b"++savecfg")))

    async def set_listen_only(self, enable):
        """
        Set the controller to liste-only mode. This will cause the controller to listen to all traffic,
        irrespective of the current address.
        """
        async with self._conn.meta["lock"]:
            await self.__write("++lon {value:d}".format(value=enable).encode('ascii'))

    async def get_listen_only(self):
        """
        Returns True if the controller is in listen-only mode.
        """
        async with self.__conn.meta["lock"]:
            return bool(int(await self.__query_command(b"++lon")))

    async def __set_device_mode(self, device_mode):
        """
        Either configure the the GPIB controller as a controller or device. The parameter is a DeviceMode enum.
        """
        assert isinstance(device_mode, DeviceMode)
        await self.__write("++mode {value:d}".format(value=device_mode.value).encode('ascii'))
        self.__state['device_mode'] = device_mode
        self.__conn.meta['device_mode'] = device_mode


class AsyncPrologixGpibEthernetController(AsyncPrologixGpib):
    def __init__(self, hostname, pad, port=1234, sad=0, timeout=13, send_eoi=1, eos_mode=0, ethernet_timeout=1000):
        conn = AsyncSharedIPConnection(hostname=hostname, port=port, timeout=(timeout+ethernet_timeout)/1000)   # timeout is in seconds
        super().__init__(
          conn=conn,
          pad=pad,
          device_mode=DeviceMode.CONTROLLER,
          timeout=timeout,
          send_eoi=send_eoi,
          eos_mode=eos_mode,
        )

    async def set_listen_only(self, enable):
        raise TypeError("Not supported in controller mode")

    async def get_listen_only(self):
        raise TypeError("Not supported in controller mode")

    async def set_status(self, value):
        raise TypeError("Not supported in controller mode")

    async def get_status(self):
        raise TypeError("Not supported in controller mode")


class AsyncPrologixGpibEthernetDevice(AsyncPrologixGpib):
    def __init__(self, hostname, pad, port=1234, sad=0, send_eoi=1, eos_mode=0, ethernet_timeout=1000):
        conn = AsyncSharedIPConnection(hostname=hostname, port=port, timeout=ethernet_timeout/1000)   # timeout is in seconds
        super().__init__(
          conn=conn,
          pad=pad,
          device_mode=DeviceMode.DEVICE,
          send_eoi=send_eoi,
          eos_mode=eos_mode,
        )

    async def set_read_after_write(self, enable):
        raise TypeError("Not supported in device mode")

    async def get_read_after_write(self):
        raise TypeError("Not supported in device mode")

    async def clear(self):
        raise TypeError("Not supported in device mode")

    async def interface_clear(self):
        raise TypeError("Not supported in device mode")

    async def remote_enable(self, enable=True):
        raise TypeError("Not supported in device mode")

    async def ibloc(self):
        raise TypeError("Not supported in device mode")

    async def read(self, len=None, character=None):
        raise TypeError("Not supported in device mode")

    async def timeout(self, value):
        raise TypeError("Not supported in device mode")

    async def serial_poll(self, pad=0, sad=0):
        raise TypeError("Not supported in device mode")

    async def test_srq(self):
        raise TypeError("Not supported in device mode")

    async def trigger(self):
        raise TypeError("Not supported in device mode")
