"""Read-only HA lifecycle for a separate SAT shadow entry. Never a coordinator."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
import re
from time import time, monotonic
from urllib.parse import urlsplit
from homeassistant.components import mqtt
from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event
from .monitor import shared_reader, transient_read_error
from .mqtt_monitor import shared_mqtt_reader, topic as mqtt_topic
from .control import stamp
from .evidence import OperatingLimits
from .kernel import Policy
from .shadow import Observer

MODE = 'ally_shadow'


async def async_reload_shadow_entry(hass, entry):
    # Use HA lifecycle so unload callbacks remove the old update listener.
    await hass.config_entries.async_reload(entry.entry_id)


def validate(settings):
    """Pure validation: do not probe MQTT or OTGW during config flow."""
    s = deepcopy(settings)
    if s.get('schema_version') != 1:
        raise ValueError('Unsupported schema')
    topic = s.get('topic', '')
    if not isinstance(topic, str) or not topic or any(c in topic for c in '+#\0'):
        raise ValueError('Exact report topic required')
    namespace = mqtt_topic(s)
    u = urlsplit(s.get('monitor_url', 'http://unused/api/v2/otgw/otmonitor' if namespace else ''))
    if (u.scheme not in ('http','https') or not u.hostname or u.username or u.password
            or u.query or u.fragment or u.path != '/api/v2/otgw/otmonitor'):
        raise ValueError('Read-only OTGW URL required')
    entity = re.compile(r'^(binary_sensor|input_boolean)\.[a-z0-9_]+$')
    if not entity.fullmatch(s.get('enabled_entity','')):
        raise ValueError('Explicit global permission required')
    members = s.get('members')
    if not isinstance(members,dict) or not members or len(members)>64:
        raise ValueError('Explicit members required')
    for ieee,m in members.items():
        if (not re.fullmatch(r'0x[0-9a-f]{16}',ieee) or not isinstance(m,dict)
                or not re.fullmatch(r'[a-z0-9_]+',m.get('name',''))
                or not isinstance(m.get('zone'),str) or not m['zone']
                or not entity.fullmatch(m.get('eligible_entity',''))):
            raise ValueError('Invalid member mapping')
    Observer(members,s['enabled_entity'],time(),Policy(**s.get('policy',{})),
             OperatingLimits(**s.get('operating_limits',{})))
    return s


class Adapter:
    def __init__(self, hass, settings):
        self.hass = hass
        self.settings = validate(settings)
        self.observer = Observer(self.settings['members'],self.settings['enabled_entity'],time(),
                                 Policy(**self.settings.get('policy',{})),
                                 OperatingLimits(**self.settings.get('operating_limits',{})))
        self.listeners = set()
        self.unsubs = []
        self.task = None
        self.closed = False
        self.connected = False
        self.generation = 0
        self.evaluated_at = None

    @property
    def attributes(self):
        now = time()
        heads = {}
        for m in self.settings['members'].values():
            raw = self.observer.reports.heads.get(m['name'], {})
            row = {'zone':m['zone'],'eligible':self.hass.states.get(m['eligible_entity']).state
                   if self.hass.states.get(m['eligible_entity']) else 'unknown'}
            for key in ('pi','heat_required'):
                reading = raw.get(key,{})
                at = reading.get('observed_at')
                row[key] = reading.get('value')
                row[key+'_reported_at'] = at
                row[key+'_age_seconds'] = max(0,round(now-stamp(at))) if at else None
            row['source'] = self.observer.reports.restore_source or 'live_reports'
            heads[m['name']] = row
        return {**self.observer.result,'heads':heads,'evaluated_at':self.evaluated_at,
                'mqtt_connected':self.connected,
                'report_errors':self.observer.reports.errors,
                'ignored_reports':self.observer.reports.ignored_reports,
                'last_ignored_device':self.observer.reports.last_ignored_device,
                'policy':asdict(self.observer.policy),
                'operating_limits':{**asdict(self.observer.limits),
                    'maximum_water':self.observer.policy.maximum_water,
                    'normal_water_guard':self.observer.policy.maximum_water + self.observer.limits.water_margin},
                'telemetry_transport':self.settings.get('telemetry_transport','http'),
                'telemetry_time_basis':'mqtt_receipt' if mqtt_topic(self.settings) else 'otgw_epoch'}

    @callback
    def notify(self):
        for listener in tuple(self.listeners): listener()

    async def start(self):
        try:
            if namespace := mqtt_topic(self.settings):
                await shared_mqtt_reader(self.hass, namespace).start()
            self.unsubs.append(await mqtt.async_subscribe(self.hass,self.settings['topic'],self.message,qos=1))
            self.unsubs.append(await mqtt.async_subscribe(self.hass,self.settings['topic'] + '/snapshot',self.snapshot_message,qos=1))
            self.unsubs.append(async_dispatcher_connect(self.hass,MQTT_CONNECTION_STATE,self.connection))
            self.unsubs.append(async_track_time_interval(self.hass,self.schedule,timedelta(seconds=30)))
            entities=[self.settings['enabled_entity']]+[m['eligible_entity'] for m in self.settings['members'].values()]
            self.unsubs.append(async_track_state_change_event(self.hass,entities,self.policy_changed))
            self.unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP,self.stop))
            self.connected = mqtt.is_connected(self.hass)
            self.schedule()
        except BaseException:
            await self.stop()
            raise

    @callback
    def snapshot_message(self, msg):
        if self.closed or not self.connected:
            return
        try:
            if self.observer.reports.accept_snapshot(json.loads(msg.payload), time(), self.observer.policy.ttl, msg.retain):
                self.schedule()
        except (ValueError, KeyError, TypeError, AttributeError):
            self.observer.disconnect(time())
            self.observer.result = self.observer._result('unknown', 'invalid_source_snapshot')
        self.notify()

    @callback
    def message(self, msg):
        if self.closed or not self.connected: return
        try: event=json.loads(msg.payload)
        except (ValueError,TypeError): event={}
        self.observer.accept(event,time(),msg.retain)
        # Decisions are evaluated at most on the next 30s poll. Do not turn
        # repeated MQTT cache publication into fresh OTGW telemetry receipts.
        self.notify()

    @callback
    def connection(self, connected):
        if self.closed: return
        self.connected = bool(connected)
        self.generation += 1
        self.observer.disconnect(time())
        self.notify()
        self.schedule()

    @callback
    def policy_changed(self, event):
        if self.closed: return
        self.generation += 1
        # Re-evaluate each zone's eligibility; do not erase unrelated hysteresis.
        # Global OFF/unknown and vetoed zones still clear in Observer.evaluate.
        self.observer.result=self.observer._result('unknown','permission_changed')
        self.notify()
        self.schedule()

    @callback
    def schedule(self, event=None):
        if self.closed or (self.task and not self.task.done()): return
        self.task=self.hass.async_create_background_task(self.refresh(),'sat ally shadow telemetry')

    async def poll(self):
        if namespace := mqtt_topic(self.settings):
            return await shared_mqtt_reader(self.hass, namespace).read()
        return await shared_reader(self.hass, self.settings['monitor_url']).read()

    async def refresh(self):
        generation=self.generation
        try:
            if not self.connected: return
            data=await self.poll()
            if self.closed or generation!=self.generation: return
            states={e:self.hass.states.get(e).state if self.hass.states.get(e) else 'unknown'
                    for e in [self.settings['enabled_entity']]+[m['eligible_entity'] for m in self.settings['members'].values()]}
            wall=time()
            self.observer.evaluate(states,data,wall,monotonic())
            self.evaluated_at=wall
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed and generation==self.generation:
                if transient_read_error(exc):
                    states={e:self.hass.states.get(e).state if self.hass.states.get(e) else 'unknown'
                            for e in [self.settings['enabled_entity']]+[m['eligible_entity'] for m in self.settings['members'].values()]}
                    self.observer.evaluate(states,None,time(),monotonic())
                else:
                    self.observer.monitor_failed()
                self.evaluated_at=time()
        finally:
            if not self.closed: self.notify()

    async def stop(self, event=None):
        if self.closed: return
        self.closed=True
        self.generation+=1
        for unsub in reversed(self.unsubs): unsub()
        self.unsubs.clear()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.listeners.clear()
        self.observer.disconnect(time())
