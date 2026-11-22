"""Shared, passive OTGW v1.7.7 canonical MQTT telemetry (explicit opt-in).

Receipt time is NOT a source measurement timestamp. No persistence, retained
restore, command publication, HTTP fallback, or command-echo-as-state shortcut.
"""
import asyncio
from copy import deepcopy
from math import isfinite
import re
from time import monotonic
from homeassistant.components import mqtt
from .evidence import MqttSnapshot
from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

KEY = 'sat_otgw_mqtt_readers'
# Canonical boiler-side worldview, not _thermostat, /override or PIC echoes.
FIELDS = {'ch_enable': ('chenable', None), 'fault': ('faultindicator', None),
          'domestichotwater': ('dhwmode', None), 'flame': ('flamestatus', None),
          'centralheating': ('chmode', None),
          'TSet': ('controlsetpoint', (0, 100)),
          'Tboiler': ('boilertemperature', (-20, 150)),
          'Toutside': ('outsidetemperature', (-60, 80)),
          'MaxRelModLevelSetting': ('maxrelmodlvl', (0, 100))}


def topic(settings):
    mode = settings.get('telemetry_transport', 'http')
    if mode not in ('http', 'mqtt'):
        raise ValueError('Unknown OTGW telemetry transport')
    if mode == 'http':
        return None
    value = settings.get('otgw_mqtt_topic', '')
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/value/[A-Za-z0-9_.-]+', value):
        raise ValueError('Explicit exact OTGW MQTT value namespace required')
    return value


class Evidence:
    def __init__(self):
        self.fields = {}
        self.sequence = 0
        self.generation = 0
        self.connected = False
        self.gateway_online = None
        self.retained_rejected = 0
        self.invalid = 0

    def reset(self, connected):
        self.fields.clear()
        self.generation += 1
        self.connected = bool(connected)
        self.gateway_online = None

    def accept(self, leaf, payload, retained, now):
        if not self.connected or not isfinite(now):
            return
        if leaf == '':
            # Retained availability can revoke trust, but cannot establish it.
            if payload == 'offline':
                self.generation += 1
                self.fields.clear()
                self.gateway_online = False
            elif payload == 'online' and not retained:
                self.generation += 1
                self.fields.clear()
                self.gateway_online = True
            return
        if leaf not in FIELDS:
            return
        if retained:
            self.retained_rejected += 1
            return
        if self.gateway_online is False:
            return
        key, bounds = FIELDS[leaf]
        try:
            if bounds is None:
                value = {'ON': 'On', 'OFF': 'Off'}[payload]
            else:
                value = float(payload)
                if not isfinite(value) or not bounds[0] <= value <= bounds[1]:
                    raise ValueError('Out of range')
        except (ValueError, TypeError, KeyError):
            self.fields.pop(key, None)
            self.invalid += 1
            return
        self.sequence += 1
        self.fields[key] = (self.sequence, value, now)

    def snapshot(self):
        return MqttSnapshot(self.fields if self.connected else {}, self.generation)


def shared_mqtt_reader(hass, namespace):
    return hass.data.setdefault(KEY, {}).setdefault(namespace, Reader(hass, namespace))


class Reader:
    """One subscription per HA/namespace, retained across consumer reloads.

    Lifetime is the HA process; HA stop releases all listeners. Lazy first-use
    is deliberate: untouched legacy HTTP settings do not subscribe.
    """
    def __init__(self, hass, namespace):
        self.hass, self.namespace = hass, namespace
        self.evidence = Evidence()
        self.lock = asyncio.Lock()
        self.unsubs = []
        self.started = False

    async def start(self):
        async with self.lock:
            if self.started:
                return
            self.evidence.reset(mqtt.is_connected(self.hass))
            try:
                self.unsubs.append(async_dispatcher_connect(self.hass, MQTT_CONNECTION_STATE, self.connection))
                self.unsubs.append(await mqtt.async_subscribe(self.hass, self.namespace + '/#', self.message, qos=0))
                self.unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.close))
                self.started = True
            except BaseException:
                self.close()
                raise

    @callback
    def connection(self, connected):
        self.evidence.reset(connected)

    @callback
    def message(self, message):
        t = message.topic
        if t == self.namespace:
            leaf = ''
        elif t.startswith(self.namespace + '/'):
            leaf = t[len(self.namespace) + 1:]
        else:
            return
        self.evidence.accept(leaf, message.payload, message.retain, monotonic())

    @callback
    def close(self, event=None):
        for unsub in reversed(self.unsubs):
            unsub()
        self.unsubs.clear()
        self.started = False
        self.evidence.reset(False)

    async def read(self):
        await self.start()
        return self.evidence.snapshot()
