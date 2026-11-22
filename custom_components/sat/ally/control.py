"""PI/HR input boundary, shared by SAT runtime and offline replay. No I/O."""
from copy import deepcopy
from datetime import datetime
from math import isfinite
from uuid import UUID
from .kernel import Head, Reading, Memory, Policy, decide


def stamp(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.utcoffset() is None:
        raise ValueError('Timezone required')
    return d.timestamp()


def evidence(raw, kinds):
    raw = raw or {}
    return Reading(raw.get('value'), stamp(raw['observed_at']) if raw.get('observed_at') else None,
                   raw.get('evidence') in kinds)


class Engine:
    """Same deterministic core in HA and replay; session/gap invalidation explicit."""
    def __init__(self, inventory, policy=None):
        if not inventory or any(not k or not v for k, v in inventory.items()):
            raise ValueError('Explicit inventory required')
        self.inventory = dict(inventory)
        self.policy = policy or Policy()
        self.reset()

    def reset(self):
        self.memory = Memory()
        self.session = self.epoch = self.last = None

    def evaluate(self, frame, now, *, enabled=True):
        if not isfinite(now) or (self.last is not None and now < self.last):
            self.reset()
            raise ValueError('Clock reversal')
        epoch = stamp(frame['session_started_at'])
        if epoch > now or set(frame.get('heads', {})) - set(self.inventory):
            raise ValueError('Invalid frame')
        if frame['session_id'] != self.session or self.last is None or now - self.last > self.policy.ttl:
            self.memory = Memory()
            self.epoch = max(epoch, now if self.last is not None and frame['session_id'] == self.session else epoch)
            self.session = frame['session_id']
        elif epoch != self.declared_epoch:
            raise ValueError('Changed session epoch')
        self.declared_epoch = epoch
        self.last = now
        heads = []
        for name, zone in self.inventory.items():
            row = frame.get('heads', {}).get(name, {})
            heads.append(Head(name, zone, evidence(row.get('pi'), {'zcl_attributeReport', 'zcl_readResponse'}),
                              evidence(row.get('heat_required'), {'zcl_attributeReport', 'zcl_readResponse'}),
                              evidence(row.get('eligible'), {'policy_evaluation'})))
        return decide(heads, evidence(frame.get('outside'), {'otgw_report'}), now, self.epoch,
                      self.memory, self.policy, enabled=enabled)


class Reports:
    """Bounded streaming Z2M evidence, never retained/HA last_updated/cache."""
    def __init__(self, mapping, started):
        if not mapping or len(set(mapping.values())) != len(mapping):
            raise ValueError('Ambiguous device map')
        self.mapping = dict(mapping)
        self.started = started
        self.snapshot_barrier = started
        self.restore_source = None
        self.epoch = started
        self.session = None
        self.session_start = None
        self.seq = 0
        self.received = None
        self.segment = 0
        self.heads = {}
        self.last_event = None
        self.restore_pending = None
        self.restored_fields = set()
        self.ignored_reports = 0
        self.last_ignored_device = None

    def invalidate(self, now):
        self.snapshot_barrier = now
        self.restore_source = None
        self.restore_pending = None
        self.restored_fields.clear()
        self.heads.clear()
        self.epoch = now
        self.segment += 1

    def snapshot(self, now):
        return deepcopy(dict(schema=1, saved_at=now, mapping=self.mapping,
            started=self.started, epoch=self.epoch, session=self.session,
            session_start=self.session_start, seq=self.seq, received=self.received,
            segment=self.segment, heads=self.heads, last_event=self.last_event))

    def stage_restore(self, data, now, ttl):
        """Quarantine disk evidence until a live, consecutive source event."""
        if self.seq or not isinstance(data, dict):
            return False
        try:
            d=deepcopy(data)
            if d['schema']!=1 or d['mapping']!=self.mapping or not 0<=now-d['saved_at']<=ttl:
                return False
            UUID(d['session'])
            if type(d['seq']) is not int or d['seq']<1 or type(d['segment']) is not int or d['segment']<0:
                return False
            if not all(type(d[k]) in (int,float) and isfinite(d[k]) for k in ('started','epoch','session_start','received','saved_at')):
                return False
            if not d['started']<=d['epoch']<=d['received']<=d['saved_at']<=now or d['session_start']>d['epoch']:
                return False
            last=d['last_event']
            if (not isinstance(last,dict) or last.get('session_id')!=d['session']
                    or last.get('sequence')!=d['seq'] or stamp(last['received_at'])!=d['received']):
                return False
            if set(d['heads'])-set(self.mapping.values()):
                return False
            for name, fields in d['heads'].items():
                if set(fields)-{'pi','heat_required'}: return False
                for key, field in fields.items():
                    at=stamp(field['observed_at']); value=field['value']
                    sid, seq=field['source_event'].rsplit(':',1)
                    if sid!=d['session'] or not 1<=int(seq)<=d['seq'] or not d['epoch']<=at<=d['received']:
                        return False
                    if field['evidence'] not in ('zcl_attributeReport','zcl_readResponse','invalid_report'): return False
                    if field['evidence']=='invalid_report' and value is not None: return False
                    if value is not None and ((key=='pi' and (type(value) is not int or not 0<=value<=100)) or (key=='heat_required' and type(value) is not bool)):
                        return False
            self.restore_pending=(d,ttl)
            return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def _resume(self, event, now, start, received, seq):
        if self.restore_pending is None: return
        d,ttl=self.restore_pending
        # A duplicate does not prove that we did not miss a newer invalidation.
        if event['session_id']==d['session'] and seq<=d['seq']: return
        self.restore_pending=None
        if (event['session_id']!=d['session'] or start!=d['session_start']
                or seq!=d['seq']+1 or received<d['received'] or now<d['saved_at']):
            return
        self.started,self.epoch=d['started'],d['epoch']
        self.session,self.session_start=d['session'],d['session_start']
        self.seq,self.received,self.segment=d['seq'],d['received'],d['segment']
        self.last_event=d['last_event']
        self.heads={name:{key:field for key,field in fields.items()
                         if 0<=now-stamp(field['observed_at'])<=ttl}
                    for name,fields in d['heads'].items()}
        self.restored_fields={(name,key) for name,fields in self.heads.items() for key in fields}

    @property
    def errors(self):
        # Bounded to configured members. Tombstones also survive cache restore.
        return {name: ('invalid_report' if any(f.get('evidence') == 'invalid_report'
                    for f in fields.values()) else 'invalid_value')
                for name, fields in self.heads.items()
                if any(f.get('value') is None for f in fields.values())}

    @staticmethod
    def _identity(e, now):
        """The source envelope belongs to the stream, not to an individual TRV."""
        if not isinstance(e, dict) or e.get('schema_version') != 2:
            raise ValueError('Event schema')
        UUID(e['session_id'])
        seq, received, start = e['sequence'], stamp(e['received_at']), stamp(e['session_started_at'])
        if (type(seq) is not int or seq < 1 or not isfinite(now)
                or not start <= received <= now
                or not isinstance(e.get('device_ieee'), str) or not 0 < len(e['device_ieee']) <= 64):
            raise ValueError('Event identity/time')
        return seq, received, start

    @staticmethod
    def _valid_fields(fields, kind):
        if (kind not in ('attributeReport', 'readResponse') or not isinstance(fields, dict)
                or not fields or set(fields) - {'pi_heating_demand', 'heat_required'}):
            return False
        for key, field in fields.items():
            if not isinstance(field, dict) or 'value' not in field or type(field.get('valid')) is not bool:
                return False
            v = field.get('value')
            if not field['valid']:
                if v is not None:
                    return False
            elif ((key == 'pi_heating_demand' and (type(v) is not int or not 0 <= v <= 100))
                  or (key == 'heat_required' and type(v) is not bool)):
                return False
        return True

    @staticmethod
    def _quarantine(sid, seq, at):
        # Negative evidence, never a fabricated measurement. A later valid PI
        # alone cannot resurrect a previous HR (or the other way around).
        return {key: dict(value=None, observed_at=at, evidence='invalid_report',
                          source_event=f'{sid}:{seq}') for key in ('pi', 'heat_required')}

    def accept_snapshot(self, data, now, ttl, retained=False):
        """Fresh source snapshot; isolate bad member rows, preserve original ages."""
        if retained:
            return False
        if (not isinstance(data, dict) or data.get('schema_version') != 1
                or data.get('complete') is not True or not isfinite(now)
                or not isfinite(ttl) or ttl <= 0):
            raise ValueError('Invalid source snapshot')
        sid = data['session_id']; UUID(sid)
        start, emitted = stamp(data['session_started_at']), stamp(data['emitted_at'])
        seq = data['sequence']
        if (type(seq) is not int or seq < 1 or not start <= emitted <= now
                or emitted < self.snapshot_barrier or now - emitted > 60):
            return False
        if self.session == sid:
            if start != self.session_start:
                raise ValueError('Changed source session start')
            if seq < self.seq:
                return False
        elif self.session_start is not None and start <= self.session_start:
            return False
        raw = data['heads']
        if not isinstance(raw, dict) or len(raw) > 64:
            raise ValueError('Unbounded source snapshot')
        last = data['last_event']
        last_seq, received, last_start = self._identity(last, emitted)
        if last['session_id'] != sid or last_seq != seq or last_start != start:
            raise ValueError('Invalid source watermark')
        heads = {}
        for ieee, name in self.mapping.items():
            if ieee not in raw:
                if self.session == sid and self.heads.get(name):
                    heads[name] = self._quarantine(sid, seq, last['received_at'])
                continue
            fields = raw[ieee]
            row = {}
            try:
                if not isinstance(fields, dict) or set(fields) - {'pi_heating_demand', 'heat_required'}:
                    raise ValueError('Invalid source fields')
                for key, field in fields.items():
                    at = stamp(field['observed_at']); n = field['sequence']
                    if (type(n) is not int or not 1 <= n <= seq or not start <= at <= received
                            or field['evidence'] not in ('zcl_attributeReport', 'zcl_readResponse')
                            or not self._valid_fields({key: field}, field['evidence'][4:])):
                        raise ValueError('Invalid source reading')
                    dest = 'pi' if key == 'pi_heating_demand' else key
                    row[dest] = dict(value=field['value'], observed_at=field['observed_at'],
                                     evidence=field['evidence'], source_event=f'{sid}:{n}')
                if ieee == last['device_ieee']:
                    if not self._valid_fields(last.get('fields'), last.get('message_type')):
                        raise ValueError('Invalid member watermark')
                    for key, field in last['fields'].items():
                        source = fields.get(key, {})
                        if (source.get('sequence') != seq or source.get('observed_at') != last['received_at']
                                or source.get('value') != field['value'] or source.get('valid') != field['valid']):
                            raise ValueError('Member watermark mismatch')
            except (ValueError, KeyError, TypeError, AttributeError):
                row = self._quarantine(sid, seq, last['received_at'])
            # A newer event/tombstone wins over an older row from a snapshot.
            # Conflicting same-sequence values poison only that member's field.
            if self.session == sid:
                for key, old in self.heads.get(name, {}).items():
                    other = row.get(key)
                    old_seq = int(old['source_event'].rsplit(':', 1)[1])
                    if other is None:
                        row[key] = self._quarantine(sid, seq, last['received_at'])[key]
                    else:
                        new_seq = int(other['source_event'].rsplit(':', 1)[1])
                        if new_seq < old_seq:
                            row[key] = deepcopy(old)
                        elif new_seq == old_seq and other != old:
                            row[key] = self._quarantine(sid, old_seq, old['observed_at'])[key]
            heads[name] = row
        self.restore_pending = None
        self.restore_source = 'verified_source_snapshot'
        self.started = min(self.started, start)
        if self.session != sid or self.epoch != start:
            self.segment += 1
        self.epoch = start
        self.session, self.session_start = sid, start
        self.seq, self.received, self.last_event = seq, received, deepcopy(last)
        self.heads = {name: {key: field for key, field in fields.items()
                            if 0 <= now - stamp(field['observed_at']) <= ttl}
                      for name, fields in heads.items()}
        self.restored_fields = {(name, key) for name, fields in self.heads.items() for key in fields}
        return True

    def accept(self, e, now, retained=False):
        if retained:
            return False
        seq, received, start = self._identity(e, now)
        if received < self.started or received < self.epoch:
            return False
        if self.restore_pending is not None and e['session_id']==self.restore_pending[0]['session'] and seq<=self.restore_pending[0]['seq']:
            return False
        self._resume(e, now, start, received, seq)
        if self.session != e['session_id']:
            if self.session_start is not None and start <= self.session_start:
                return False
            self.invalidate(max(self.started, start))
            self.session, self.session_start, self.seq, self.received = e['session_id'], start, 0, None
        elif self.session_start != start:
            raise ValueError('Changed session start')
        if seq <= self.seq:
            if seq == self.seq and e != self.last_event:
                # A conflict attributable to the SAME TRV cannot discard other
                # rooms. A changed identity is still a stream integrity fault.
                if e['device_ieee'] != self.last_event['device_ieee']:
                    raise ValueError('Conflicting duplicate identity')
                name = self.mapping.get(e['device_ieee'])
                if name:
                    self.heads[name] = self._quarantine(self.session, seq, self.last_event['received_at'])
                    self.restored_fields = {(n, k) for n, k in self.restored_fields if n != name}
                return True
            return False
        if self.received is not None and received < self.received:
            raise ValueError('Source clock reversal')
        if self.seq and seq > self.seq + 1:
            # An actual missing stream event could have been ANY room's OFF.
            self.invalidate(received)
        self.seq, self.received, self.last_event = seq, received, deepcopy(e)
        name = self.mapping.get(e['device_ieee'])
        if name is None:
            self.ignored_reports += 1
            self.last_ignored_device = e['device_ieee']
            return True  # consume global watermark; no membership or heat credit
        fields = e.get('fields')
        if not self._valid_fields(fields, e.get('message_type')):
            self.heads[name] = self._quarantine(self.session, seq, e['received_at'])
            self.restored_fields = {(n, k) for n, k in self.restored_fields if n != name}
            return True
        for key, field in fields.items():
            dest = 'pi' if key == 'pi_heating_demand' else key
            self.restored_fields.discard((name, dest))
            self.heads.setdefault(name, {})[dest] = {
                'value': field['value'], 'observed_at': e['received_at'],
                'evidence': 'zcl_' + e['message_type'], 'source_event': f'{self.session}:{seq}'}
        return True


class Ramp:
    """Monotonic minimum OFF time; loss of evidence always overrides minimum run."""
    def __init__(self, minimum, maximum, now, restart_delay=180, rise_per_minute=2):
        self.minimum, self.maximum = minimum, maximum
        self.delay, self.rate = restart_delay, rise_per_minute
        self.off_at = self.last = now
        self.on = False
        self.water = minimum

    def stop(self, now):
        if self.on:
            self.off_at = now
        self.on = False
        self.water = self.minimum
        self.last = now

    def target(self, decision, now):
        if now < self.last:
            raise ValueError('Monotonic reversal')
        if decision.heat is not True:
            self.stop(now)
            return None
        if not self.on and now - self.off_at < self.delay:
            return None
        water = min(self.maximum, max(self.minimum, decision.water))
        if self.on:
            water = min(water, self.water + min(60, now - self.last) * self.rate / 60)
        else:
            water = self.minimum
        self.on, self.water, self.last = True, round(water, 2), now
        return self.water
