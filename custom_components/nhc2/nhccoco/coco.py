import json
import logging
import os
import threading
from time import sleep
from typing import Callable

import paho.mqtt.client as mqtt

from .coco_energy_home import CoCoEnergyHome

from .coco_device_class import CoCoDeviceClass
from .coco_fan import CoCoFan
from .coco_light import CoCoLight
from .coco_switch import CoCoSwitch
from .coco_switched_fan import CoCoSwitchedFan
from .coco_climate import CoCoThermostat
from .coco_energy import CoCoEnergyMeter
from .coco_cover import CoCoCover
from .coco_accesscontrol import CoCoAccessControl
from .coco_button import CoCoButton
from .coco_smartplug import CoCoSmartPlug
from .coco_generic import CoCoGeneric
from .coco_virtual import CoCoVirtual

from .const import *
from .helpers import *

_LOGGER = logging.getLogger(__name__)
sem = threading.Semaphore()

DEVICE_SETS = {
    CoCoDeviceClass.SWITCHED_FANS: {INTERNAL_KEY_CLASS: CoCoSwitchedFan, INTERNAL_KEY_MODELS: LIST_VALID_SWITCHED_FANS},
    CoCoDeviceClass.FANS: {INTERNAL_KEY_CLASS: CoCoFan, INTERNAL_KEY_MODELS: LIST_VALID_FANS},
    CoCoDeviceClass.COVERS: {INTERNAL_KEY_CLASS: CoCoCover, INTERNAL_KEY_MODELS: LIST_VALID_COVERS},
    CoCoDeviceClass.SWITCHES: {INTERNAL_KEY_CLASS: CoCoSwitch, INTERNAL_KEY_MODELS: LIST_VALID_SWITCHES},
    CoCoDeviceClass.LIGHTS: {INTERNAL_KEY_CLASS: CoCoLight, INTERNAL_KEY_MODELS: LIST_VALID_LIGHTS},
    CoCoDeviceClass.THERMOSTATS: {INTERNAL_KEY_CLASS: CoCoThermostat, INTERNAL_KEY_MODELS: LIST_VALID_THERMOSTATS},
    CoCoDeviceClass.ENERGYMETERS: {INTERNAL_KEY_CLASS: CoCoEnergyMeter, INTERNAL_KEY_MODELS: LIST_VALID_ENERGYMETERS},
    CoCoDeviceClass.ACCESSCONTROL: {INTERNAL_KEY_CLASS: CoCoAccessControl, INTERNAL_KEY_MODELS: LIST_VALID_ACCESSCONTROL},
    CoCoDeviceClass.BUTTONS: {INTERNAL_KEY_CLASS: CoCoButton, INTERNAL_KEY_MODELS: LIST_VALID_BUTTONS},
    CoCoDeviceClass.SMARTPLUGS: {INTERNAL_KEY_CLASS: CoCoSmartPlug, INTERNAL_KEY_MODELS: LIST_VALID_SMARTPLUGS},
    CoCoDeviceClass.VIRTUAL: {INTERNAL_KEY_CLASS: CoCoVirtual, INTERNAL_KEY_MODELS: LIST_VALID_VIRTUAL},
    CoCoDeviceClass.ENERGY_HOME: {INTERNAL_KEY_CLASS: CoCoEnergyHome, INTERNAL_KEY_MODELS: LIST_VALID_ENERGY_HOME}
}


class CoCo:
    def __init__(self, address, username, password, port=8884, ca_path=None, switches_as_lights=False):

        if switches_as_lights:
            DEVICE_SETS[CoCoDeviceClass.LIGHTS] = {INTERNAL_KEY_CLASS: CoCoLight,
                                                   INTERNAL_KEY_MODELS: LIST_VALID_LIGHTS + LIST_VALID_SWITCHES}
            DEVICE_SETS[CoCoDeviceClass.SWITCHES] = {INTERNAL_KEY_CLASS: CoCoSwitch, INTERNAL_KEY_MODELS: []}
        # The device control buffer fields
        self._keep_thread_running = True
        self._device_control_buffer = {}
        self._device_control_buffer_size = DEVICE_CONTROL_BUFFER_SIZE
        self._device_control_buffer_command_size = DEVICE_CONTROL_BUFFER_COMMAND_SIZE
        self._device_control_buffer_command_count = 0
        self._device_control_buffer_thread = threading.Thread(target=self._publish_device_control_commands)
        self._device_control_buffer_thread.start()

        if ca_path is None:
            ca_path = os.path.dirname(os.path.realpath(__file__)) + MQTT_CERT_FILE
        client = mqtt.Client(protocol=MQTT_PROTOCOL, transport=MQTT_TRANSPORT)
        client.username_pw_set(username, password)
        client.tls_set(ca_path)
        client.tls_insecure_set(True)
        self._client = client
        self._address = address
        self._port = port
        self._profile_creation_id = username
        self._all_devices = None
        self._device_callbacks = {}
        self._devices = {}
        self._devices_callback = {}
        self._system_info = None
        self._system_info_callback = lambda x: None

    def __del__(self):
        self._keep_thread_running = False
        self._client.disconnect()

    def connect(self, on_connection_refused=None):

        def _on_message(client, userdata, message):
            topic = message.topic
            response = json.loads(message.payload)

            if topic == self._profile_creation_id + MQTT_TOPIC_PUBLIC_RSP and \
                    response[KEY_METHOD] == MQTT_METHOD_SYSINFO_PUBLISH:
                self._system_info = response
                self._system_info_callback(self._system_info)

            elif topic == (self._profile_creation_id + MQTT_TOPIC_SUFFIX_RSP) and \
                    response[KEY_METHOD] == MQTT_METHOD_DEVICES_LIST:
                self._client.unsubscribe(self._profile_creation_id + MQTT_TOPIC_SUFFIX_RSP)
                self._process_devices_list(response)

            elif topic == (self._profile_creation_id + MQTT_TOPIC_SUFFIX_SYS_EVT) and \
                    response[KEY_METHOD] == MQTT_METHOD_SYSINFO_PUBLISHED:
                # If the connected controller publishes sysinfo... we expect something to have changed.
                client.subscribe(self._profile_creation_id + MQTT_TOPIC_SUFFIX_RSP, qos=1)
                client.publish(self._profile_creation_id + MQTT_TOPIC_SUFFIX_CMD,
                               json.dumps({KEY_METHOD: MQTT_METHOD_DEVICES_LIST}), 1)

            elif topic == (self._profile_creation_id + MQTT_TOPIC_SUFFIX_EVT) and \
                    (response[KEY_METHOD] == MQTT_METHOD_DEVICES_STATUS or response[KEY_METHOD] == MQTT_METHOD_DEVICES_CHANGED):
                devices = extract_devices(response)
                
                for device in devices:
                    try:
                        if KEY_UUID in device:
                            self._device_callbacks[device[KEY_UUID]][INTERNAL_KEY_CALLBACK](device)
                    except Exception as e:
                        _LOGGER.warning(f'Failed to invoke callback: {e}')
                        pass

        def _on_connect(client, userdata, flags, rc):
            if rc == 0:
                _LOGGER.info('Connected')
                client.subscribe(self._profile_creation_id + MQTT_TOPIC_SUFFIX_RSP, qos=1)
                client.subscribe(self._profile_creation_id + MQTT_TOPIC_PUBLIC_RSP, qos=1)
                client.subscribe(self._profile_creation_id + MQTT_TOPIC_SUFFIX_EVT, qos=1)
                client.subscribe(self._profile_creation_id + MQTT_TOPIC_SUFFIX_SYS_EVT, qos=1)
                client.publish(self._profile_creation_id + MQTT_TOPIC_PUBLIC_CMD,
                               json.dumps({KEY_METHOD: MQTT_METHOD_SYSINFO_PUBLISH}), 1)
                client.publish(self._profile_creation_id + MQTT_TOPIC_SUFFIX_CMD,
                               json.dumps({KEY_METHOD: MQTT_METHOD_DEVICES_LIST}), 1)

            elif rc in [1, 2, 3, 4, 5] and on_connection_refused is not None:
                # Possible reasons for Connection refused:
                # 1: Connection refused - incorrect protocol version
                # 2: Connection refused - invalid client identifier
                # 3: Connection refused - server unavailable
                # 4: Connection refused - bad username or password
                # 5: Connection refused - not authorised
                on_connection_refused(rc)
            elif MQTT_RC_CODES[rc]:
                raise Exception(MQTT_RC_CODES[rc])
            else:
                raise Exception('Unknown error')

        def _on_disconnect(client, userdata, rc):
            _LOGGER.warning('Disconnected')
            for uuid, device_callback in self._device_callbacks.items():
                if device_callback[INTERNAL_KEY_CALLBACK]:
                    offline = {'Online': 'False', KEY_UUID: uuid}
                    device_callback[INTERNAL_KEY_CALLBACK](offline)
                else:
                    _LOGGER.info(f'No callback for device with UUID {uuid}')

        self._client.on_message = _on_message
        self._client.on_connect = _on_connect
        self._client.on_disconnect = _on_disconnect

        self._client.connect_async(self._address, self._port)
        self._client.loop_start()

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def get_systeminfo(self, callback):
        self._system_info_callback = callback
        if self._system_info:
            self._system_info_callback(self._system_info)

    def get_devices(self, device_class: CoCoDeviceClass, callback: Callable):
        self._devices_callback[device_class] = callback
        if self._devices and device_class in self._devices: 
            self._devices_callback[device_class](self._devices[device_class])

    def _publish_device_control_commands(self):
        while self._keep_thread_running:
            device_commands_to_process = None
            sem.acquire()
            if len(self._device_control_buffer.keys()) > 0:
                device_commands_to_process = self._device_control_buffer
            self._device_control_buffer = {}
            self._device_control_buffer_command_count = 0
            sem.release()
            if device_commands_to_process is not None:
                command = process_device_commands(device_commands_to_process)
                #_LOGGER.debug(json.dumps(command))
                self._client.publish(self._profile_creation_id + MQTT_TOPIC_SUFFIX_CMD, json.dumps(command), 1)
            sleep(0.05)

    def _add_device_control(self, uuid, property_key, property_value):
        while len(self._device_control_buffer.keys()) >= self._device_control_buffer_size or \
                self._device_control_buffer_command_count >= self._device_control_buffer_command_size:
            pass
        sem.acquire()
        self._device_control_buffer_command_count += 1
        if uuid not in self._device_control_buffer:
            self._device_control_buffer[uuid] = {}
        self._device_control_buffer[uuid][property_key] = property_value
        sem.release()

    # Processes response on devices.list
    def _process_devices_list(self, response):
        # Only add devices that are actionable
        actionable_devices = list(
            filter(lambda d: d[KEY_TYPE] == DEV_TYPE_ACTION, extract_devices(response)))
        actionable_devices.extend(list(
            filter(lambda d: d[KEY_TYPE] == "thermostat", extract_devices(response))))
        actionable_devices.extend(list(
            filter(lambda d: d[KEY_TYPE] == "centralmeter", extract_devices(response))))
        actionable_devices.extend(list(
            filter(lambda d: d[KEY_TYPE] == "smartplug", extract_devices(response))))
        actionable_devices.extend(list(
            filter(lambda d: d[KEY_TYPE] == "virtual", extract_devices(response))))
        actionable_devices.extend(list(
            filter(lambda d: d[KEY_TYPE] == "energyhome", extract_devices(response))))

        # Only prepare for devices that don't already exist
        # TODO - Can't we do this when we need it (in initialize_devices ?)
        existing_uuids = list(self._device_callbacks.keys())
        for actionable_device in actionable_devices:
            if actionable_device[KEY_UUID] not in existing_uuids:
                self._device_callbacks[actionable_device[KEY_UUID]] = \
                    {INTERNAL_KEY_CALLBACK: None, KEY_ENTITY: None}

        # Initialize
        self.initialize_devices(CoCoDeviceClass.ENERGY_HOME, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.SWITCHED_FANS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.FANS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.SWITCHES, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.LIGHTS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.COVERS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.THERMOSTATS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.ENERGYMETERS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.ACCESSCONTROL, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.BUTTONS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.SMARTPLUGS, actionable_devices)
        self.initialize_devices(CoCoDeviceClass.VIRTUAL, actionable_devices)

    def initialize_devices(self, device_class, actionable_devices):

        base_devices = [x for x in actionable_devices if x[KEY_MODEL]
                        in DEVICE_SETS[device_class][INTERNAL_KEY_MODELS]]
        if device_class not in self._devices:
            self._devices[device_class] = []
        for base_device in base_devices:
            if self._device_callbacks[base_device[KEY_UUID]] and self._device_callbacks[base_device[KEY_UUID]][KEY_ENTITY] and \
                    self._device_callbacks[base_device[KEY_UUID]][KEY_ENTITY].uuid:
                self._device_callbacks[base_device[KEY_UUID]][KEY_ENTITY].update_dev(base_device)
            else:
                self._device_callbacks[base_device[KEY_UUID]][KEY_ENTITY] = \
                    DEVICE_SETS[device_class][INTERNAL_KEY_CLASS](base_device,
                                                                  self._device_callbacks[base_device[KEY_UUID]],
                                                                  self._client,
                                                                  self._profile_creation_id,
                                                                  self._add_device_control)
                self._devices[device_class].append(self._device_callbacks[base_device[KEY_UUID]][KEY_ENTITY])
        if device_class in self._devices_callback:
            self._devices_callback[device_class](self._devices[device_class])
