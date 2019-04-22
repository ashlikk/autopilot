"""

Classes that manage hardware logic.

Each hardware class should be able to operate independently - ie. not
be dependent on a particular task class, etc. Other than that there are
very few design requirements:

* Every class should have a .release() method that releases any system
  resources in use by the object, eg. objects that use pigpio must have
  their `pigpio.pi` client stopped; LEDs should be explicitly turned off.
* The very minimal class attributes are described in the :class:`Hardware` metaclass.
* Hardware methods are typically called in their own threads, so care should
  be taken to make any long-running operations internally threadsafe.

Note:
    This software was primarily developed for the Raspberry Pi, which
    has `two types of numbering schemes <https://pinout.xyz/#>`_ ,
    "board" numbering based on physical position and "bcm" numbering
    based on the broadcom chip numbering scheme.

    Board numbering is easier to use, but `pigpio <http://abyz.me.uk/rpi/pigpio/>`_
    , which we use as a bridge between Python and the GPIOs, uses the BCM scheme.
    As such each class that uses the GPIOs takes a board number as its argument
    and converts it to a BCM number in the __init__ method.

    If there is sufficient demand to make this more flexible, we can implement
    an additional `pref` to set the numbering scheme, but the current solution
    works without getting too muddy.

Warning:
    In order to use pigpio, the pigpio daemon must be running. See `the docs <http://abyz.me.uk/rpi/pigpio/python.html>`_
    Usually :class:`~.core.pilot.Pilot` s should be started by the bash script or systemd service
    generated by :mod:`.setup.setup_pilot`, which starts pigpiod.
"""
#
# try:
#     import RPi.GPIO as GPIO
# except:
#     pass

#try:

from rpilot import prefs
from rpilot.core.networking import Net_Node
if prefs.AGENT in ['pilot']:
    import pigpio

    TRIGGER_MAP = {
        'U': pigpio.RISING_EDGE,
        'D': pigpio.FALLING_EDGE,
        'B': pigpio.EITHER_EDGE
    }
    """
    dict: Maps strings ('U', 'D', 'B') to pigpio edge types
    (RISING_EDGE, FALLING_EDGE, EITHER_EDGE), respectively.
    """

    PULL_MAP = {
        'U': pigpio.PUD_UP,
        'D': pigpio.PUD_DOWN
    }
# TODO: needs better handling, pigpio crashes sometimes and we should know
#except ImportError:
#    pass

try:
    import usb
except ImportError:
    pass

from inputs import devices


import threading
import time
from datetime import datetime
import numpy as np
from Queue import Queue, Empty


# pigpio only uses BCM numbers, we need to translate them
# See https://www.element14.com/community/servlet/JiveServlet/previewBody/73950-102-11-339300/pi3_gpio.png
BOARD_TO_BCM = {
     3: 2,   5: 3,   7: 4,   8: 14, 10: 15,
    11: 17, 12: 18, 13: 27, 15: 22, 16: 23,
    18: 24, 19: 10, 21: 9,  22: 25, 23: 11,
    24: 8,  26: 7,  29: 5,  31: 6,  32: 12,
    33: 13, 35: 19, 36: 16, 37: 26, 38: 20,
    40: 21
}
"""
dict: Mapping from board (physical) numbering to BCM numbering. 

See `this pinout <https://pinout.xyz/#>`_.

Hardware objects take board numbered pins and convert them to BCM 
numbers for use with `pigpio`.
"""

BCM_TO_BOARD = dict([reversed(i) for i in BOARD_TO_BCM.items()])
"""
dict: The inverse of :const:`BOARD_TO_BCM`.
"""



class Hardware(object):
    """
    Generic class inherited by all hardware. Should not be instantiated
    on its own (but it won't do anything bad so go nuts i guess).

    Primarily for the purpose of defining necessary attributes.

    Also defines `__del__` to call `release()` so objects are always released
    even if not explicitly.

    Attributes:
        trigger (bool): Is this object a discrete event input device?
            or, will this device be used to trigger some event? If `True`,
            will be given a callback by :class:`.Task`, and :meth:`.assign_cb`
            must be redefined.
        pin (int): The BCM pin used by this device, or None if no pin is used.
        type (str): What is this device known as in `.prefs`? Not required.
        input (bool): Is this an input device?
        output (bool): Is this an output device?
    """
    # metaclass for hardware objects
    trigger = False
    pin = None
    type = "" # what are we known as in prefs?
    input = False
    output = False

    def release(self):
        """
        Every hardware device needs to redefine `release()`, and must

        * Safely unload any system resources used by the object, and
        * Return the object to a neutral state - eg. LEDs turn off.

        When not redefined, a warning is given.
        """
        Warning('The release method was not overridden by the subclass!')

    def assign_cb(self, trigger_fn):
        """
        Every hardware device that is a :attr:`~Hardware.trigger` must redefine this
        to accept a function (typically :meth:`.Task.handle_trigger`) that
        is called when that trigger is activated.

        When not redefined, a warning is given.
        """
        if self.trigger:
            Warning("The assign_cb method was not overridden by the subclass!")

    def __del__(self):
        self.release()

# TODO: Subclass nosepoke that knows about waiting for mouse leaving
class Beambreak(Hardware):
    """
    An IR Beambreak sensor.

    A phototransistor that changes voltage from 'high' to 'low' or vice versa
    when light is blocked.

    Attributes:
        pig (:meth:`pigpio.pi`): The pigpio connection.
        pin (int): Broadcom-numbered pin, converted from the argument given on instantiation
        callbacks (list): A list of :meth:`pigpio.callback`s kept to clear them on exit
    """
    trigger=True
    type = 'POKES'
    input = True

    def __init__(self, pin, pull_ud='U', trigger_ud='D', event=None):
        """
        Args:
            pin (int): Board-numbered pin, converted to BCM numbering during instantiation.
            pull_ud ('U', 'D', 'B'): Should this beambreak be pulled up or down?
            trigger_ud ('U', 'D', 'B'): Is the trigger event up (low to high) or down (high to low)?
            event (:class:`threading.Event`): We can be passed an Event object if we want to handle
                stage transition logic here instead of the :class:`.Task` object, as is typical.
        """

        self.trigger = True
        self.type = 'POKES'
        self.input = True

        # Make pigpio instance
        self.pig = pigpio.pi()

        # Convert pin from board to bcm numbering
        self.pin = BOARD_TO_BCM[int(pin)]

        try:
            self.pull_ud = PULL_MAP[pull_ud]
        except KeyError:
            Exception('pull_ud must be one of {}, was given {}'.format(PULL_MAP.keys(), pull_ud))

        try:
            self.trigger_ud = TRIGGER_MAP[trigger_ud]
        except KeyError:
            Exception('trigger_ud must be one of {}, was given {}'.format(TRIGGER_MAP.keys(), trigger_ud))

        # We can be passed a threading.Event object if we want to handle stage logic here
        # rather than in the parent as is typical.
        self.event = event

        # List to store callback handles
        self.callbacks = []

        # Setup pin
        self.pig.set_mode(self.pin, pigpio.INPUT)
        self.pig.set_pull_up_down(self.pin, self.pull_ud)

    def __del__(self):
        self.pig.stop()

    def release(self):
        """
        Simply calls `self.pig.stop()` to release pigpio resources.
        """
        self.pig.stop()

    def assign_cb(self, callback_fn, add=False, evented=False, manual_trigger=None):
        """
        Sets `callback_fn` to be called when triggered.

        Args:
            callback_fn (callable): The function to be called when triggered
            add (bool): Are we adding another callback?
                If False, the previous callbacks are cleared.
            evented (bool): Should triggering this event also set the internal :attr:`~.Beambreak.event`?
                Note that :attr:`.Beambreak.event` must have been passed.
            manual_trigger ('U', 'D', 'B'): We can override :attr:`.Beambreak.trigger_ud` if needed.
        """
        # If we aren't adding, we clear any existing callbacks
        if not add:
            self.clear_cb()

        # We can set the direction of the trigger manually,
        # for example if we want to set 'BOTH' only sometimes
        if not manual_trigger:
            trigger_ud = self.trigger_ud
        else:
            trigger_ud = TRIGGER_MAP[manual_trigger]

        # We can handle eventing (blocking) here if we want (usually this is handled in the parent)
        # This won't work if we weren't init'd with an event.
        if evented and self.event:
            cb = self.pig.callback(self.pin, trigger_ud, self.event.set)
            self.callbacks.append(cb)
        elif evented and not self.event:
            Exception('We have no internal event to set!')

        cb = self.pig.callback(self.pin, trigger_ud, callback_fn)
        self.callbacks.append(cb)

    def clear_cb(self):
        """
        Tries to call `.cancel()` on each of the callbacks in :attr:`Beambreak.callbacks`
        """
        for cb in self.callbacks:
            try:
                cb.cancel()
            except:
                pass
        self.callbacks = []

class Flag(Beambreak):
    """
    Trivial Reclass of the Beambreak class with the default directions reversed.

    TODO:
        Need to add argument passing into hardware spec so we don't need stuff like this


    """

    def __init__(self, pin):
        super(Flag, self).__init__(pin, pull_ud="D", trigger_ud="U")




class LED_RGB(Hardware):
    """
    An RGB LED.

    Attributes:
        pig (:meth:`pigpio.pi`): The pigpio connection.
        flash_block (:class:`threading.Event`): An Event to wait on setting further colors
            if we are currently in a threaded flash train
        pins (dict): After init, pin numbers are kept in a dict like::

            {'r':bcm_number, 'g':...}

        stored_color (dict): A color we store to restore after we do a flash train.

    """

    output = True
    type="LEDS"

    def __init__(self, pins = None, r = None, g=None, b=None, common = 'anode', blink=True):
        """
        Args:
            pins (list): A list of (board) pin numbers.
                Either `pins` OR all `r`, `g`, `b` must be passed.
            r (int): Board number of Red pin - must be passed with `g` and `b`
            g (int): Board number of Green pin - must be passed with `r` and `b`
            b (int): Board number of Blue pin - must be passed with `r` and `g`:
            common ('anode', 'cathode'): Is this LED common anode (low turns LED on)
                or cathode (low turns LED off)
            blink (bool): Flash RGB at the end of init to show we're alive.
        """
        self.common = common

        # Dict to store color for after flash trains
        self.stored_color = {}

        # Event to wait on setting colors if we're flashing
        self.flash_block = threading.Event()
        self.flash_block.set()

        # Initialize connection to pigpio daemon
        self.pig = pigpio.pi()
        if not self.pig.connected:
            Exception('No connection to pigpio daemon could be made')

        # Unpack input
        self.pins = {}
        if r and g and b:
            self.pins['r'] = int(r)
            self.pins['g'] = int(g)
            self.pins['b'] = int(b)
        elif isinstance(pins, list):
            self.pins['r'] = int(pins[0])
            self.pins['g'] = int(pins[1])
            self.pins['b'] = int(pins[2])
        else:
            Exception('Dont know how to handle input to LED_RGB')

        # Convert to BCM numbers
        self.pins = {k: BOARD_TO_BCM[v] for k, v in self.pins.items()}

        # set pin mode to output and make sure they're turned off
        for pin in self.pins.values():
            self.pig.set_mode(pin, pigpio.OUTPUT)
            if self.common == 'anode':
                self.pig.set_PWM_dutycycle(pin, 255)
            elif self.common == 'cathode':
                self.pig.set_PWM_dutycycle(pin, 0)
            else:
                Exception('Common passed to LED_RGB not anode or cathode')

        # Blink to show we're alive
        if blink:
            self.color_series([[255,0,0],[0,255,0],[0,0,255],[0,0,0]], 250)

    def __del__(self):
        self.pig.stop()

    def release(self):
        """
        Turns LED off and releases pigpio.
        """
        self.set_color(col=[0,0,0])
        self.pig.stop()

    def set_color(self, col=None, r=None, g=None, b=None, timed=None, stored=False, internal=False):
        """
        Set the color of the LED.

        Note:
            if called during a :meth:`LED_RGB.color_series`, the color will be stashed and set when the train is over.

        Args:
            col (list, tuple): an RGB color trio ranging from 0-255. Either `col` or all of `r`, `g`, `b` must be provided
            r (int): Red intensity 0-255. Must be passed with `g` and `b`
            g (int): Green intensity 0-255. Must be passed with `r` and `b`
            b (int): Blue intensity 0-255. Must be passed with `r` and `g`
            timed (float): Duration to change to this color before turning off in ms.
            stored (bool): Called internally to change back to the color that preceded a flash train. Restores :attr:`LED_RGB.stored_color`.
            internal (bool): True if being called inside a flash train.
        """
        if stored:
            # being called after a flash train
            # Since this is always called after a flash train, check that we were actually assigned a color
            if self.stored_color:
                color = self.stored_color
                self.stored_color = {}
            else:
                # It's fine not to have a color, just return quietly.
                return
        else:
            # Unpack input
            if r and g and b:
                color = {'r':int(r), 'g':int(g), 'b':int(b)}
            elif isinstance(col, list) or isinstance(col, tuple):
                color = {'r':int(col[0]), 'g':int(col[1]), 'b':int(col[2])}
            else:
                Warning('Color improperly formatted')
                return

        # If we're flashing or doing a color series, stash the color and we'll set it after the flash is done
        # the 'internal' flag checks if this is being called within a flash train
        if not internal and not self.flash_block.is_set():
            self.stored_color = color
            return

        # Set PWM dutycycle
        if self.common == 'anode':
            for k, v in color.items():
                self.pig.set_PWM_dutycycle(self.pins[k], 255-v)
        elif self.common == 'cathode':
            for k, v in color.items():
                self.pig.set_PWM_dutycycle(self.pins[k], v)

        # If this is is a timed blink, start thread to turn led off
        if timed:
            # timed should be a float or int specifying the delay in ms
            offtimer = threading.Timer(float(timed)/1000, self.set_color, kwargs={'col':[0,0,0]})
            offtimer.start()

    def flash(self, duration, frequency=20, colors=[[255,255,255],[0,0,0]]):
        """
        Specify a color series by total duration and flash frequency.

        Largely a convenience function for on/off flashes.

        Args:
            duration (int, float): Duration of flash in ms.
            frequency (int, float): Frequency of flashes in Hz
            colors (list): A list of RGB values 0-255 like::

                [[255,255,255],[0,0,0]]

        """
        # Duration is total in ms, frequency in Hz
        # Get number of flashes in duration rounded down
        n_rep = int(float(duration)/1000.*float(frequency))
        flashes = colors*n_rep

        # Invert frequency to duration for single flash
        single_dur = (1./frequency)*1000
        self.color_series(flashes, single_dur)

    def color_series(self, colors, duration):
        """
        Change color through a series for a fixed duration.

        Wrapper around :meth:`LED_RGB.threaded_color_series`

        Args:
            colors (list): A list of RGB values 0-255 like::

                [[255,255,255],[0,0,0]]

            duration (int, list): Either a single duration (int, ms)
                or list of ints of equal length to `colors` to define
                duration for each.
        """
        # Just a wrapper to make threaded
        series_thread = threading.Thread(target=self.threaded_color_series, kwargs={'colors':colors, 'duration':duration})
        series_thread.start()

    def threaded_color_series(self, colors, duration):
        """
        Should only be called by :meth:`.LED_RGB.color_series` because it blocks.

        Clears :attr:`.LED_RGB.flash_block` , sets colors, sleeps, sets the block, and
        then sets any color that was passed during the train.

        Args:
            colors (list): A list of RGB values 0-255 like::

                [[255,255,255],[0,0,0]]

            duration (int, list): Either a single duration (int, ms)
                or list of ints of equal length to `colors` to define
                duration for each.
        """
        self.flash_block.clear()
        if isinstance(duration, int) or isinstance(duration, float):
            for c in colors:
                self.set_color(c, internal=True)
                time.sleep(float(duration)/1000)
        elif isinstance(duration, list) and (len(colors) == len(duration)):
            for i, c in enumerate(colors):
                self.set_color(c, internal=True)
                time.sleep(float(duration[i])/1000)
        else:
            Exception("Dont know how to handle your color series")
            return
        self.flash_block.set()
        # If we received a color command while we were doing the series, set it now.
        # We call the function regardless, it will switch to a color if it has one
        self.set_color(stored=True)

class Solenoid(Hardware):
    """
    Solenoid valves for water delivery.

    Only NC solenoids should be used, as there is no way to guarantee
    that a pin will maintain its voltage when it is released, and you will
    spill water all over the place.

    Note:
        pigpio has a function to send waveforms, which would make solenoid
        opening far more accurate. If you are using an audio device, however,
        creating and sending waveforms disables it. Waveforms are thus not
        implemented here, but their implementation is left, skeleton-like,
        in the source should you do an experiment without audio that
        needs more precision.

        It's hard to see why submillisecond precision
        would matter all that much for reward delivery, but such is the
        obsessiveness of scientists.
    """

    output = True
    type = "PORTS"

    def __init__(self, pin, duration=100):
        """
        Args:
            pin (int): Board pin number, converted to BCM on init.
            duration (int, float): duration of open, ms.
        """

        # Initialize connection to pigpio daemon
        self.pig = pigpio.pi()
        if not self.pig.connected:
            Exception('No connection to pigpio daemon could be made')

        # Setup port
        self.pin = BOARD_TO_BCM[int(pin)]
        self.pig.set_mode(self.pin, pigpio.OUTPUT)

        # Pigpio has us create waves to deliver timed output
        # Since we typically only use one duration,
        # we make the wave once and only make it again when asked to
        # We start with passed or default duration (ms)
        self.duration = float(duration)/1000
        #self.wave_id = None
        #self.make_wave()

    def __del__(self):
        self.pig.stop()

    def release(self):
        """
        Simply releases the pigpio resources
        """
        self.pig.stop()
    #
    # def make_wave(self, duration=None):
    #     """
    #     Args:
    #         duration:
    #     """
    #
    #     # Typically duration is stored as an attribute, but if we are passed one...
    #     if duration:
    #         self.duration = int(duration)
    #
    #     # Make a pulse (duration is in microseconds for pigpio, ours is in milliseconds
    #     # Pulses are (pin to turn on, pin to turn off, delay)
    #     # So we add two pulses, one to turn the pin on with a delay,
    #     # then a second to turn the pin off with no delay.
    #     reward_pulse = []
    #     reward_pulse.append(pigpio.pulse(1<<self.pin, 0, self.duration*1000))
    #     reward_pulse.append(pigpio.pulse(0, 1<<self.pin, 0))
    #
    #     self.pig.wave_add_generic(reward_pulse)
    #     self.wave_id = self.pig.wave_create()

    def open(self, duration=None):
        """
        Open the valve.

        Args:
            duration (float): If provided, open for this duration instead of
                the duration stored on instantiation.
        """
        if duration:
            try:
                duration = float(duration)
            except:
                Warning('Need to pass a float for duration, using default dur')
                duration = self.duration
        else:
            duration = self.duration

        #self.pig.wave_send_once(self.wave_id)
        self.pig.write(self.pin, 1)
        time.sleep(duration)
        self.pig.write(self.pin, 0)


class Wheel(Hardware):
    """
    A continuously measured mouse wheel.

    Uses a USB computer mouse.

    Warning:
        'vel' thresh_type not implemented
    """

    input   = True
    type    = "Wheel"
    trigger = False # even though this is a triggerable option, typically don't want to assign a cb and instead us a GPIO
    # TODO: Make the standard-style trigger.
    # TODO: Make wheel movements available locally with a deque

    THRESH_TYPES = ['dist', 'x', 'y', 'vel']

    MODES = ('vel_total', 'steady', 'dist', 'timed')

    MOVE_DTYPE = [('vel', 'i4'), ('dir', 'U5'), ('timestamp', 'f8')]

    def __init__(self, mouse_idx=0, fs=20, thresh=100, thresh_type='dist', start=True,
                 gpio_trig=False, pins=None, mode='vel_total', integrate_dur=3):

        # try to get mouse from inputs
        # TODO: More robust - specify mouse by hardware attrs
        try:
            self.mouse = devices.mice[mouse_idx]
        except IndexError:
            Warning('Could not find requested mouse with index {}\nAttempting to use mouse idx 0'.format(mouse_idx))
            self.mouse = devices.mice[0]

        # frequency of our updating
        self.fs = fs
        # time between updates
        self.update_dur = 1./float(self.fs)

        self.thresh = thresh
        self.thresh_val = 0.0

        # thresh type can be 'dist', 'x', 'y', or 'vel'
        if thresh_type not in self.THRESH_TYPES:
            ValueError('thresh_type must be one of {}, given {}'.format(self.THRESH_TYPES, thresh_type))
        self.thresh_type = thresh_type

        # mode can be 'vel_total', 'vel_x', 'vel_y' or 'dist' - report either velocity or distance
        # mode can also be '
        # TODO: Do two parameters - type 'vel' or 'dist' and measure 'x', 'y', 'total'z
        self.mode = mode
        # TODO: Implement this

        self.integrate_dur = integrate_dur


        # event to signal quitting
        self.quit_evt = threading.Event()
        self.quit_evt.set()
        # event to signal when to start accumulating movements to trigger
        self.measure_evt = threading.Event()
        self.measure_time = 0
        # queue to I/O mouse movements summarized at fs Hz
        self.q = Queue()
        # lock to prevent race between putting and getting
        self.qlock = threading.Lock()

        self.listens = {'MEASURE':self.l_measure,
                        'CLEAR':self.l_clear,
                        'STOP':self.l_stop}
        self.node = Net_Node('wheel_{}'.format(mouse_idx),
                             upstream=prefs.NAME,
                             port=prefs.MSGPORT,
                             listens=self.listens,
                             )

        # if we are being used in a child object, we send our trigger via a GPIO pin
        self.gpio_trig = gpio_trig
        self.pins = pins
        if self.gpio_trig:
            self.pig = pigpio.pi()

            pins_temp = pins
            self.pins = {}
            for k, v in pins_temp.items():
                self.pins[k] = BOARD_TO_BCM[int(v)]
                self.pig.set_mode(self.pins[k], pigpio.OUTPUT)
                self.pig.write(self.pins[k], 0)




        self.thread = None

        if start:
            self.start()


    def start(self):
        self.thread = threading.Thread(target=self._record)
        self.thread.daemon = True
        self.thread.start()

    def _record(self):
        moves = np.array([], dtype=self.MOVE_DTYPE)

        last_update = time.time()

        while self.quit_evt:

            events = self.mouse.read()


            # make a numpy record array of events with 3 fields:
            # velocity, dir(ection), timestamp (system seconds)
            move = np.array([(int(event.state), event.code, float(event.timestamp))\
                             for event in events if event.code in ('REL_X', 'REL_Y')],
                            dtype=self.MOVE_DTYPE)
            moves = np.concatenate([moves, move])

            # If we have been told to start measuring for a trigger...
            if self.measure_evt:
                do_trigger = self.check_thresh(move)
                if do_trigger:
                    self.thresh_trig()
                    self.measure_evt.clear()
                # take the integral of velocities



            # If it's time to report velocity, do it.
            nowtime = time.time()
            if (nowtime-last_update)>self.update_dur:

                # TODO: Implement distance/position reporting
                y_vel = self.calc_move(moves, 'y')
                x_vel = self.calc_move(moves, 'x')

                self.node.send(key='CONTINUOUS', value={'x':x_vel, 'y':y_vel, 't':nowtime})

                moves = np.array([], dtype=self.MOVE_DTYPE)

                last_update = nowtime

    def check_thresh(self, move):
        """
        Updates thresh_val and checks whether it's above/below threshold

        Args:
            move (np.array): Structured array with fields ('vel', 'dir', 'timestamp')

        Returns:

        """

        # Determine whether the threshold was surpassed
        do_trigger = False
        if self.mode == 'vel_total':
            thresh_update = self.calc_move(move)
            # If instantaneous velocity is above thresh...
            if thresh_update > self.thresh:
                do_trigger = True

        elif self.mode == 'steady':
            # If movements in the recent past are below a certain value
            # self.thresh_val should be set to a structured array by l_measure
            self.thresh_val = np.concatenate(self.thresh_val, move)
            # trim to movements in the time window
            self.thresh_val = self.thresh_val[self.thresh_val['timestamp'] > time.time()-self.integrate_dur]

            thresh_update = self.calc_move(move)

            if thresh_update < self.thresh:
                do_trigger = True

        elif self.mode == 'dist':
            thresh_update = self.calc_move(move)
            self.thresh_val += thresh_update

            if self.thresh_val > self.thresh:
                do_trigger = True

        else:
            Warning ("mode is not defined! mode is {}".format(self.mode))

        return do_trigger

    def calc_move(self, move, thresh_type=None):
        """
        Calculate distance move depending on type (x, y, total dist)

        Args:
            move ():
            thresh_type ():

        Returns:

        """

        if thresh_type is None:
            thresh_type = self.thresh_type

        # FIXME: rly inefficient
        # get the value of the movement depending on what we're measuring
        if thresh_type == 'x':

            distance = np.sum(move['vel'][move['dir'] == "REL_X"])
        elif thresh_type == 'y':
            distance = np.sum(move['vel'][move['dir'] == "REL_Y"])
        elif thresh_type == "dist":
            x_dist = np.sum(move['vel'][move['dir'] == "REL_X"])
            y_dist = np.sum(move['vel'][move['dir'] == "REL_Y"])
            distance = np.sqrt(float(x_dist ** 2) + float(y_dist ** 2))

        return distance

    def thresh_trig(self, which=None):

        if not which:
            if self.gpio_trig:
                for pin in self.pins.keys():
                    self.pig.gpio_trigger(pin, 100, 1)

        else:
            if self.gpio_trig:
                self.pig.gpio_trigger(self.pins[which], 100, 1)

        self.measure_evt.clear()




    def assign_cb(self, trigger_fn):
        # want to have callback write an output pin -- so callback should go back to
        # the task to write a GPIO pin.
        self.trig_fn = trigger_fn

    def l_measure(self, value):
        """
        Task has signaled that we need to start measuring movements for a trigger


        Args:
            value ():
        """

        if 'mode' in value.keys():
            if value['mode'] in self.MODES:
                self.mode = value['mode']
            else:
                Warning('incorrect mode sent: {}, needs to be one of {}'.format(value['mode'], self.MODES))

        if 'thresh' in value.keys():
            self.thresh = float(value['thresh'])

        if self.mode == "steady":
            self.thresh_val = np.array([], dtype=self.MOVE_DTYPE)
        else:
            self.thresh_val = 0.0
        self.measure_time = time.time()

        self.measure_evt.set()

    def l_clear(self, value):
        """
        Stop measuring!

        Args:
            value ():

        Returns:

        """
        self.measure_evt.clear()

    def l_stop(self, value):
        """
        Stop measuring and clear system resources
        Args:
            value ():

        Returns:

        """

        self.measure_evt.clear()
        self.release()

    def release(self):
        self.quit_evt.clear()



class Scale(Hardware):
    """
    Note:
        Not implemented, working on using a digital scale to
        make weighing faster.
    """
    MODEL={
        'stamps.com':{
            'vendor_id':0x1446,
            'product_id': 0x6a73

        }
    }
    def __init__(self, model='stamps.com', vendor_id = None, product_id = None):
        """
        Args:
            model:
            vendor_id:
            product_id:
        """
        self.vendor_id = self.MODEL[model]['vendor_id']
        self.product_id = self.MODEL[model]['product_id']

        if vendor_id:
            self.vendor_id = vendor_id
        if product_id:
            self.product_id = product_id

        # find device
        self.device = usb.core.find(idVendor=self.vendor_id,
                                    idProduct=self.product_id)
        # default configuration
        self.device.set_configuration()

class Pull(Hardware):
    """
    Pull a pin up or down. Called by the :class:`~.pilot.Pilot` instead of by
    a :class:`~.task.Task` as is usual.

    If a pin should be pulled up or down always, regardless of task,
    use this. For example, the `otherwise wonderful HiFiBerry Amp 2 <https://www.hifiberry.com/shop/boards/hifiberry-amp2/>`_
    has an undocumented ... feature ... : the (board) pin 8 mutes output when low.
    """

    def __init__(self, pin, pud):
        """
        Args:
            pin (int): (Board) pin number
            pud ('U', 'D'): Pull the pin 'U'p or 'D'own.
        """
        self.pig = pigpio.pi()
        if not self.pig.connected:
            Exception('No connection to pigpio daemon could be made')

        self.pin = BOARD_TO_BCM[int(pin)]

        if pud == 1:
            self.pig.set_pull_up_down(self.pin, pigpio.PUD_UP)
        elif pud == 0:
            self.pig.set_pull_up_down(self.pin, pigpio.PUD_DOWN)



    def __del__(self):
        self.pig.stop()

    def release(self):
        """
        Simply releases the pigpio client.
        """
        self.pig.stop()









