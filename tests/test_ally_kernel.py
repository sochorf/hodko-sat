"""Synthetic offline contract tests; no device access or calibration claims."""
import importlib.util
import sys
import unittest
from pathlib import Path

from custom_components.sat.ally import kernel as m
R, H = m.Reading, m.Head


def head(name='WR', zone='WR', pi=100, required=True, eligible=True, at=1000, verified=True):
    return H(name, zone, R(pi, at, verified), R(required, at, verified), R(eligible, at, verified))


class DemandTests(unittest.TestCase):
    def run_step(self, heads, now=1000, memory=None, **kw):
        return m.decide(heads, R(9.3, now, True), now, 100,
                        memory if memory is not None else m.Memory(), **kw)

    def test_demand_without_room_temperature(self):
        d = self.run_step([head()])
        self.assertTrue(d.heat)
        self.assertEqual(d.water, 38.42)

    def test_night_wr_off_dominates_residual_pi(self):
        d = self.run_step([head(pi=39, required=False)])
        self.assertFalse(d.heat)
        self.assertIsNone(d.water)

    def test_other_room_still_requests(self):
        d = self.run_step([head(required=False), head('BR', 'BR', 25)])
        self.assertTrue(d.heat)

    def test_stale_pi_not_refreshed_by_other_fields(self):
        h = head()
        h = H(h.name, h.zone, R(100, 100, True), R(True, 4000, True), R(True, 4000, True))
        d = self.run_step([h], now=4000)
        self.assertIsNone(d.heat)
        self.assertEqual(d.reasons['WR'], 'pi_unknown')

    def test_retained_or_unverified_never_arms(self):
        self.assertIsNone(self.run_step([head(at=99)]).heat)
        self.assertIsNone(self.run_step([head(verified=False)]).heat)

    def test_unknown_not_zero_but_valid_room_can_heat(self):
        self.assertIsNone(self.run_step([head(pi=None)]).heat)
        d = self.run_step([head(pi=None), head('BR', 'BR', 40)])
        self.assertTrue(d.heat)
        self.assertTrue(d.degraded)

    def test_disabled_dominates_and_clears_memory(self):
        mem = m.Memory({'WR': True}, {'WR': 100})
        self.assertFalse(self.run_step([head()], memory=mem, enabled=False).heat)
        self.assertEqual(mem.calling, {})

    def test_zero_is_valid(self):
        self.assertFalse(self.run_step([head(pi=0)]).heat)

    def test_closed_window_excludes_old_positive(self):
        self.assertFalse(self.run_step([head(eligible=False, pi=100)]).heat)

    def test_hysteresis(self):
        mem = m.Memory()
        self.assertTrue(self.run_step([head(pi=25)], memory=mem).heat)
        self.assertTrue(self.run_step([head(pi=15)], now=1001, memory=mem).heat)
        self.assertFalse(self.run_step([head(pi=10)], now=1002, memory=mem).heat)

    def test_low_demand_eventually_served_without_wait_reset(self):
        mem = m.Memory()
        self.assertFalse(self.run_step([head(pi=9)], memory=mem).heat)
        self.assertFalse(self.run_step([head(pi=9)], now=1599, memory=mem).heat)
        self.assertTrue(self.run_step([head(pi=9)], now=1600, memory=mem).heat)
        self.assertTrue(self.run_step([head(pi=9)], now=1601, memory=mem).heat)

    def test_one_percent_residual_does_not_start(self):
        mem = m.Memory()
        self.run_step([head(pi=1)], memory=mem)
        self.assertFalse(self.run_step([head(pi=1, at=2000)], now=2000, memory=mem).heat)

    def test_unknown_gap_resets_wait(self):
        mem = m.Memory()
        self.run_step([head(pi=9)], memory=mem)
        self.run_step([head(pi=None)], now=1500, memory=mem)
        self.assertFalse(self.run_step([head(pi=9, at=1600)], now=1600, memory=mem).heat)

    def test_pr_two_heads_not_double_weight(self):
        a = self.run_step([head('PR1', 'PR', 80)])
        b = self.run_step([head('PR1', 'PR', 80), head('PR2', 'PR', 80)])
        self.assertEqual(a.water, b.water)
        self.assertEqual(b.zones, {'PR': True})

    def test_missing_outside_does_not_guess(self):
        d = m.decide([head()], R(None, None), 1000, 100, m.Memory())
        self.assertIsNone(d.heat)
        self.assertIsNone(d.water)

    def test_water_cap(self):
        d = m.decide([head()], R(-30, 1000, True), 1000, 100, m.Memory())
        self.assertEqual(d.water, 45)

    def test_invalid_future_and_nan(self):
        for value in [float('nan'), float('inf'), -1, 101, True]:
            self.assertIsNone(self.run_step([head(pi=value)]).heat)
        self.assertIsNone(self.run_step([head(at=1001)]).heat)

    def test_duplicate_and_time_reversal_rejected(self):
        with self.assertRaises(ValueError):
            self.run_step([head(), head()])
        mem = m.Memory()
        self.run_step([head()], memory=mem)
        with self.assertRaises(ValueError):
            self.run_step([head()], now=999, memory=mem)

    def test_empty_inventory_unknown(self):
        self.assertIsNone(self.run_step([]).heat)


class SustainedDemandTests(unittest.TestCase):
    def policy(self):
        return m.Policy(ttl=3900, minimum_water=41, maximum_water=50,
                        water_warm=41, water_cold=45)

    def step(self, mem, t, pi=92, required=True, eligible=True, at=None,
             outside=True, heads=None):
        h = heads if heads is not None else [head(pi=pi, required=required,
                    eligible=eligible, at=t if at is None else at)]
        return m.decide(h, R(12.6, t, outside), t, 100, mem, self.policy())

    def warmed(self, end=2800):
        mem=m.Memory()
        for t in range(1000, end+1, 30):
            d=self.step(mem,t)
        return mem,d

    def test_configurable_55_cap_and_correction_preserve_report_expiry(self):
        p=m.Policy(ttl=3900, minimum_water=41, maximum_water=55,
                   water_warm=41, water_cold=50, sustained_max=5)
        mem=m.Memory()
        for t in range(1000,3401,30):
            d=m.decide([head(pi=100,at=1000)],R(-10,t,True),t,100,mem,p)
            self.assertLessEqual(d.water,55)
        self.assertEqual(d.water,55)
        self.assertEqual(d.corrections['WR'],5)
        d=m.decide([head(pi=100,at=1000)],R(-10,4901,True),4901,100,mem,p)
        self.assertIsNone(d.heat)

    def test_configurable_60_cap_and_correction_preserve_report_expiry(self):
        p=m.Policy(ttl=3900, minimum_water=41, maximum_water=60,
                   water_warm=41, water_cold=50, sustained_max=10)
        mem=m.Memory()
        for t in range(1000,4601,30):
            d=m.decide([head(pi=100,at=1000)],R(-10,t,True),t,100,mem,p)
            self.assertLessEqual(d.water,60)
        self.assertEqual(d.water,60)
        self.assertEqual(d.corrections['WR'],10)
        d=m.decide([head(pi=100,at=1000)],R(-10,4901,True),4901,100,mem,p)
        self.assertIsNone(d.heat)

    def test_delay_then_rate_and_cap(self):
        mem=m.Memory()
        for t in range(1000,1601,30):
            d=self.step(mem,t)
            self.assertEqual(d.water,45.98)
        d=self.step(mem,1630)
        self.assertAlmostEqual(d.corrections['WR'],0.1)
        self.assertEqual(d.water,46.08)
        for t in range(1660,4001,30):
            d=self.step(mem,t)
            self.assertLessEqual(d.water,50)
        self.assertEqual(d.water,50)
        self.assertEqual(d.corrections['WR'],5)

    def test_verified_sparse_pi_can_ramp_only_until_original_expiry(self):
        mem=m.Memory()
        for t in range(1000,4901,30):
            d=self.step(mem,t,at=1000)
            self.assertLessEqual(d.water,50)
        self.assertEqual(d.corrections['WR'],5)
        # Neither loop execution nor recent HR may re-age the old PI.
        h=head(at=1000)
        h=H(h.name,h.zone,h.pi,R(True,4930,True),R(True,4930,True))
        d=self.step(mem,4930,heads=[h])
        self.assertIsNone(d.heat)
        self.assertEqual(d.corrections,{})

    def test_explicit_shorter_correction_age_remains_supported(self):
        mem=m.Memory();p=m.Policy(ttl=3900,sustained_pi_age=600)
        for t in range(1000,2501,30):
            d=m.decide([head(at=1000)],R(8,t,True),t,100,mem,p)
            self.assertEqual(d.corrections['WR'],0)

    def test_restore_does_not_credit_unobserved_high_demand_time(self):
        mem=m.Memory()
        d=self.step(mem,2800,at=1000)
        self.assertEqual(d.corrections['WR'],0)
        for t in range(2830,3401,30):d=self.step(mem,t,at=1000)
        self.assertEqual(d.corrections['WR'],0)
        d=self.step(mem,3430,at=1000)
        self.assertAlmostEqual(d.corrections['WR'],0.1)

    def test_oct22_wr_source_times_after_1718_recovery(self):
        from datetime import datetime
        stamp=lambda v: datetime.fromisoformat(v).timestamp()
        start=stamp('2026-10-22T15:18:16+00:00')
        pi_at=stamp('2026-10-22T14:52:39.625+00:00')
        hr_at=stamp('2026-10-22T15:04:03.736+00:00')
        mem=m.Memory()
        for elapsed in range(0,1801,30):
            t=start+elapsed
            h=H('WR','WR',R(100,pi_at,True),R(True,hr_at,True),R(True,t,True))
            d=m.decide([h],R(8,t,True),t,0,mem,self.policy())
            self.assertLessEqual(d.water,50)
            if elapsed<=600:self.assertEqual(d.corrections['WR'],0)
        self.assertEqual(d.corrections['WR'],4)

    def test_decay_and_hysteresis(self):
        mem,d=self.warmed()
        self.assertEqual(d.corrections['WR'],4)
        d=self.step(mem,2830,pi=75)
        self.assertEqual(d.corrections['WR'],4)
        d=self.step(mem,2860,pi=60)
        self.assertAlmostEqual(d.corrections['WR'],3.85)
        # Returning high must satisfy the full delay again.
        d=self.step(mem,2890)
        self.assertAlmostEqual(d.corrections['WR'],3.85)

    def test_block_off_invalid_and_gap_reset(self):
        for change in [{'required':False},{'eligible':False},
                       {'pi':None},{'outside':False}]:
            with self.subTest(change=change):
                mem,_=self.warmed()
                self.step(mem,2830,**change)
                d=self.step(mem,2860)
                self.assertEqual(d.corrections['WR'],0)
        mem,_=self.warmed()
        d=self.step(mem,3000)
        self.assertEqual(d.corrections['WR'],0)

    def test_pr_pair_not_double_boost_and_partial_data_allowed(self):
        mem=m.Memory()
        for t in range(1000,2201,30):
            hs=[head('a','PR',at=t),head('b','PR',at=t),
                head('c','WR',pi=None,at=t)]
            d=self.step(mem,t,heads=hs)
        self.assertEqual(d.corrections,{'PR':2.0})
        self.assertTrue(d.heat)
        self.assertTrue(d.degraded)
        self.assertEqual(d.water,48.38)

    def test_correction_not_transferred_between_rooms(self):
        mem,_=self.warmed()
        d=self.step(mem,2830,heads=[head('BR','BR',at=2830)])
        self.assertEqual(d.corrections,{'BR':0})
        self.assertNotIn('WR',mem.sustained)

    def test_poll_frequency_independence(self):
        results=[]
        for interval in (10,30,60):
            mem=m.Memory()
            for t in range(1000,2201,interval):
                # Reports every five minutes; evaluations are not new measurements.
                at=1000+((t-1000)//300)*300
                d=self.step(mem,t,at=at)
            results.append(d.water)
        self.assertEqual(results,[47.98]*3)

    def test_invalid_parameters(self):
        for args in [{'sustained_start':101},{'sustained_delay':0},
                     {'sustained_gap':0},{'sustained_rise':-1},
                     {'sustained_pi_age':0},{'sustained_max':-1}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.Policy(**args)


if __name__ == '__main__':
    unittest.main()
