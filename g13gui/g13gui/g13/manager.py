#!/usr/bin/python

import enum
import errno
import queue
import threading
import time
import usb.core
import usb.util
import traceback

from evdev import UInput
from evdev import AbsInfo
from evdev import ecodes as e

from g13gui.model.bindings import StickMode
from g13gui.g13.common import G13NormalKeys
from g13gui.g13.common import G13SpecialKeys


class G13Endpoints(enum.Enum):
    KEY = 1
    LCD = 2


VENDOR_ID = 0x046D
PRODUCT_ID = 0xC21C
REPORT_SIZE = 8
LCD_BUFFER_SIZE = 0x3C0

KEYS = {}
KEYS.update(e.KEY)
del KEYS[e.KEY_MAX]
del KEYS[e.KEY_CNT]

UINPUT_KEYBOARD_CAPS = {
    e.EV_KEY: KEYS,
    e.EV_ABS: [
        (e.ABS_X, AbsInfo(value=0, min=0, max=255,
                          fuzz=0, flat=0, resolution=0)),
        (e.ABS_Y, AbsInfo(0, 0, 255, 0, 0, 0))
    ]
}


class StateError(RuntimeError):
    pass


class Manager(threading.Thread):
    class State(enum.Enum):
        DISCOVERING = 0
        FOUND = 1
        SHUTDOWN = 2

    def __init__(self, prefs):
        threading.Thread.__init__(self, daemon=True)
        self._prefs = prefs
        self._state = Manager.State.DISCOVERING
        self._device = None
        self._uinput = UInput(UINPUT_KEYBOARD_CAPS,
                              name='G13 Keyboard',
                              version=0x1,
                              vendor=VENDOR_ID,
                              product=PRODUCT_ID)
        self._lastKeyState = {}
        self._commandQueue = queue.Queue()

    @property
    def state(self):
        return self._state

    def _ensureState(self, state):
        if self._state != state:
            raise StateError()

    def _reset(self):
        try:
            self._device.reset()
        except usb.core.USBError as err:
            print('Couldn\'t reset device: %s' % (err))

        usb.util.dispose_resources(self._device)
        del self._device
        self._device = None
        time.sleep(1)

    def _discover(self):
        if self._device:
            self._reset()

        self._state = Manager.State.DISCOVERING

        while self._state == Manager.State.DISCOVERING:
            try:
                while not self._device:
                    self._device = usb.core.find(idVendor=VENDOR_ID,
                                                 idProduct=PRODUCT_ID)
                    if not self._device:
                        time.sleep(1)

                self._device.reset()
                if self._device.is_kernel_driver_active(0):
                    self._device.detach_kernel_driver(0)
                cfg = usb.util.find_descriptor(self._device)
                self._device.set_configuration(cfg)
            except usb.core.USBError as err:
                print('Unable to discover device: %s' % (err))
                traceback.print_exc()
                self._reset()
            else:
                self._state = Manager.State.FOUND

    def _readKeys(self, buffer):
        # Apparently an "interrupt" read with the G13 "times out" if no keys
        # have been pressed, and this is apparently not an error. On the
        # upside, it means we can poll the device periodically and mostly
        # sleep in the kernel waiting for the interrupt.

        try:
            return self._device.read(
                usb.util.CTRL_IN | G13Endpoints.KEY.value,
                buffer, timeout=100)
        except usb.core.USBError as err:
            if err.errno == errno.ETIMEDOUT:
                return 0
            else:
                raise

    class LEDBits(enum.Enum):
        M1 = 1
        M2 = 2
        M3 = 4
        MR = 8

    def setLedsMode(self, leds):
        """Sets the LEDs under the M* keys

        leds: a bitwise-or'd bitfield of LEDBits. Set is on.
        """
        self._ensureState(Manager.State.FOUND)
        self._commandQueue.put([self._setLedsMode, (leds,)])

    def _setLedsMode(self, leds):
        data = [5, leds, 0, 0, 0]
        type = usb.util.CTRL_TYPE_CLASS | usb.util.CTRL_RECIPIENT_INTERFACE

        self._device.ctrl_transfer(type,
                                   bRequest=9,
                                   wValue=0x305,
                                   wIndex=0,
                                   data_or_wLength=data)

    def setBacklightColor(self, r, g, b):
        """Sets the backlight color.

        r, g, b: byte values between 0-255
        """
        self._ensureState(Manager.State.FOUND)
        self._commandQueue.put([self._setBacklightColor, (r, g, b)])

    def _setBacklightColor(self, r, g, b):
        data = [5, r, g, b, 0]
        type = usb.util.CTRL_TYPE_CLASS | usb.util.CTRL_RECIPIENT_INTERFACE

        self._device.ctrl_transfer(
            type, bRequest=9, wValue=0x307, wIndex=0,
            data_or_wLength=data)

    def setLCDBuffer(self, buffer):
        """Updates the LCD screen with the contents of buffer.

        Note: buffer must be a byte array containing an LPBM formatted image.
        IOW, each byte represents one vertical row of 8 pixels each.
        """
        self._ensureState(Manager.State.FOUND)
        self._commandQueue.put([self._setLCDBuffer, (buffer,)])

    def _setLCDBuffer(self, buffer):
        header = [0] * 32
        header[0] = 0x03

        self._device.write(
            usb.util.CTRL_OUT | G13Endpoints.LCD.value, header + buffer)

    def _processCommands(self):
        while True:
            try:
                (fn, args) = self._commandQueue.get_nowait()
                fn(*args)
                self._commandQueue.task_done()
            except queue.Empty:
                break

    def run(self):
        reportBuffer = usb.util.create_buffer(REPORT_SIZE)

        while self._state != Manager.State.SHUTDOWN:
            print('Discovering devices')
            self._discover()
            print('Got device')

            while self._state == Manager.State.FOUND:
                try:
                    count = self._readKeys(reportBuffer)

                    if count == REPORT_SIZE:
                        self._synthesizeKeys(reportBuffer)
                        self._synthesizeStick(reportBuffer)
                        self._uinput.syn()

                    self._processCommands()

                except usb.core.USBError as err:
                    print('Unexpected error occurred: %s' % err)
                    break

        print('Shutting down')
        if self._device and self._state == Manager.State.FOUND:
            self._reset()

    def _synthesizeStick(self, report):
        (joy_x, joy_y) = report[1:3]
        stickMode = self._prefs.selectedProfile().stickMode

        if stickMode == StickMode.KEYS:
            regions = self._prefs.selectedProfile().stickRegions()
            joy_x = joy_x / 255
            joy_y = joy_y / 255

            for name, region in regions.items():
                binding = self._prefs.selectedProfile().keyBinding(name)
                wasPressed = self._lastKeyState.get(name, False)
                inX = (joy_x >= region[0] and joy_x <= region[2])
                inY = (joy_y >= region[1] and joy_y <= region[3])
                nowPressed = inX and inY

                if not wasPressed and nowPressed:
                    for code in binding:
                        self._uinput.write(e.EV_KEY, code, 1)
                elif wasPressed and not nowPressed:
                    for code in binding:
                        self._uinput.write(e.EV_KEY, code, 0)

                self._lastKeyState[name] = nowPressed

        elif stickMode == StickMode.RELATIVE:
            print('Relative stick mode is not implemented yet!')

        elif stickMode == StickMode.ABSOLUTE:
            self._uinput.write(e.EV_ABS, e.ABS_X, joy_x)
            self._uinput.write(e.EV_ABS, e.ABS_Y, joy_y)

    def _synthesizeKeys(self, report):
        for key in G13NormalKeys:
            binding = self._prefs.selectedProfile().keyBinding(key)
            wasPressed = self._lastKeyState.get(key, False)
            nowPressed = key.testReport(report)

            if not wasPressed and nowPressed:
                for code in binding:
                    self._uinput.write(e.EV_KEY, code, 1)
            elif wasPressed and not nowPressed:
                for code in binding:
                    self._uinput.write(e.EV_KEY, code, 0)

            self._lastKeyState[key] = nowPressed

    def signalSpecialKeys(self, report):
        for key in G13SpecialKeys:
            wasPressed = self._lastKeyState.get(key, False)
            nowPressed = key.testReport(report)

            # Emit special keypress if and only if it was released
            if wasPressed and not nowPressed:
                pass

            self._lastKeyState[key] = nowPressed

    def shutdown(self):
        self._state = Manager.State.SHUTDOWN
