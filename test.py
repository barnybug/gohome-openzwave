#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""

This file is part of **python-openzwave** project https://github.com/OpenZWave/python-openzwave.
    :platform: Unix, Windows, MacOS X
    :sinopsis: openzwave wrapper

.. moduleauthor:: bibi21000 aka Sébastien GALLET <bibi21000@gmail.com>

License : GPL(v3)

**python-openzwave** is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

**python-openzwave** is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
You should have received a copy of the GNU General Public License
along with python-openzwave. If not, see http://www.gnu.org/licenses.

"""

# Must set /usr/config/config.xml NetworkKey

import logging
import sys, os

#logging.getLogger('openzwave').addHandler(logging.NullHandler())
#logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)-10s %(message)s')

logger = logging.getLogger('openzwave')

import openzwave
from openzwave.controller import ZWaveController
from openzwave.network import ZWaveNetwork
from openzwave.option import ZWaveOption
import time
from pydispatch import dispatcher

device = "/dev/ttyACM0"
log = "None"
sniff = 300.0

for arg in sys.argv:
    if arg.startswith("--device"):
        temp,device = arg.split("=")
    elif arg.startswith("--log"):
        temp,log = arg.split("=")
    elif arg.startswith("--sniff"):
        temp,sniff = arg.split("=")
        sniff = float(sniff)
    elif arg.startswith("--help"):
        print("help : ")
        print("  --device=/dev/yourdevice ")
        print("  --log=Info|Debug")

#Define some manager options
options = ZWaveOption(device, \
  config_path="/home/barnaby/virtualenvs/openzwave/lib/python3.6/site-packages/python_openzwave/ozw_config", \
  user_path=".", cmd_line="")
options.set_log_file("OZW_Log.log")
options.set_append_log_file(False)
options.set_console_output(False)
options.set_save_log_level(log)
options.set_logging(True)
options.lock()

LOCK_ALARM_TYPE = {
    9: 'Deadbolt Jammed',
    18: 'Locked with Keypad by user ',
    19: 'Unlocked with Keypad by user ',
    21: 'Manually Locked ',
    22: 'Manually Unlocked ',
    24: 'Locked by RF',
    25: 'Unlocked by RF',
    27: 'Auto re-lock',
    33: 'User deleted: ',
    112: 'Master code changed or User added: ',
    113: 'Duplicate Pin-code: ',
    130: 'RF module, power restored',
    161: 'Tamper Alarm: ',
    167: 'Low Battery',
    168: 'Critical Battery Level',
    169: 'Battery too low to operate'
}

def network_started(network):
    logger.info("network started: homeid {:08x} - {} nodes were found.".format(network.home_id, network.nodes_count))

def network_failed(network):
    logger.info("network failed")

def network_ready(network):
    logger.info("network ready: %d nodes were found", network.nodes_count)
    logger.info("network ready: controller is %s", network.controller)

def node_update(network, node):
    logger.info("node update: %s", node)

def value_update(network, node, value):
    logger.info("value update: %s", value)
    if value.label == 'Alarm Type':
        logger.info("Alarm: %s", LOCK_ALARM_TYPE.get(value.data))

def ctrl_message(state, message, network, controller):
    logger.info('controller message: %s', message)

# Create a network object
network = ZWaveNetwork(options, autostart=False)

dispatcher.connect(network_started, ZWaveNetwork.SIGNAL_NETWORK_STARTED)
dispatcher.connect(network_failed, ZWaveNetwork.SIGNAL_NETWORK_FAILED)
dispatcher.connect(network_ready, ZWaveNetwork.SIGNAL_NETWORK_READY)

network.start()

# wait for the network.
logger.info("***** Waiting for network to become ready:")
for i in range(60):
    if network.state == network.STATE_READY:
        break
    sys.stdout.write(".")
    sys.stdout.flush()
    time.sleep(1.0)

# connect to updates after initialization has finished
dispatcher.connect(node_update, ZWaveNetwork.SIGNAL_NODE)
dispatcher.connect(value_update, ZWaveNetwork.SIGNAL_VALUE)
dispatcher.connect(ctrl_message, ZWaveController.SIGNAL_CONTROLLER)

def nodes_matching_class(name):
    return filter(lambda n: name in n.command_classes_as_string, network.nodes.values())

import pdb; pdb.set_trace()

network.stop()
