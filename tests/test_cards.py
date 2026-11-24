"""Each real card's recorded answers, parsed, against what perccli showed in the same minute; and
the cases those cards didn't show, made from them by changing bytes."""

import re
import struct
import unittest

from lightweight_perc import PercError, read_controller
from lightweight_perc.parse import (OFF_PD_BAD_BLOCK_TABLE_FULL, OFF_PD_DDF_TYPE,
                                   OFF_PD_DISABLED_FOR_REMOVAL, OFF_PD_PATH_BROKEN,
                                   OFF_PD_TEMPERATURE, OFF_PD_UNUSABLE,
                                   OFF_PD_FW_STATE, OFF_PD_MEDIA_ERR, OFF_PD_OTHER_ERR,
                                   OFF_PD_PRED_FAIL, OFF_PD_PROG_ACTIVE, OFF_PD_PROG_REBUILD,
                                   OFF_TEMPERATURE_ROC, parse_bbu_status, parse_config,
                                   parse_ctrl_info, parse_ld_list, parse_pd_info, parse_pd_list)
from lightweight_perc.testing import CARDS, ReplayCard, answers, load

LINE = re.compile(r'^(\w+)(?:\{(.*)\})? (\S+)$')


def expected(card):
    """perccli's metrics in the same minute, as {(name, labels): value}."""
    out = {}
    for line in load(card)["perccli_metrics"]["lines"]:
        name, labels, value = LINE.match(line).groups()
        out[(name, tuple(sorted(re.findall(r'(\w+)="([^"]*)"', labels or ""))))] = float(value)
    return out


def as_perccli(c):
    """The reading in the shape of perccli's metrics: each disk the card has, and each array."""
    out = {("perc_up", ()): 1.0,
           ("perc_controller_info", (("model", c.model),)): 1.0}
    for t, a in c.arrays.items():
        out[("perc_vd_state", tuple(sorted({"vd": str(t), "name": a.name, "type": a.raid_type,
                                            "state": a.state_name}.items())))] = 1.0
        out[("perc_vd_size_bytes", tuple(sorted({"vd": str(t), "name": a.name}.items())))] = a.size_bytes
    for d in c.disks.values():
        vd = "" if d.array is None else str(d.array)
        out[("perc_pd_state", tuple(sorted({"enclosure": str(d.enclosure), "slot": str(d.slot),
                                            "vd": vd, "model": d.model, "state": d.state_name}.items())))] = 1.0
        out[("perc_pd_size_bytes", (("enclosure", str(d.enclosure)), ("slot", str(d.slot))))] = d.size_bytes
    return out


def read(card_answers, **kw):
    card = ReplayCard(card_answers, kw.pop("status", None))
    return read_controller(card, **kw), card.sent


class Cards(unittest.TestCase):
    def test_every_card_matches_perccli(self):
        """Names, labels and states exactly; sizes within 0.1% (perccli rounds them)."""
        for name in CARDS:
            with self.subTest(card=name):
                got = as_perccli(read(answers(name), battery=False)[0])
                want = expected(name)
                self.assertEqual(set(got), set(want))
                for key, value in want.items():
                    if key[0].endswith("_bytes"):
                        self.assertAlmostEqual(got[key] / value, 1.0, delta=0.001, msg=key)
                    else:
                        self.assertEqual(got[key], value, msg=key)

    def test_each_disks_error_counts(self):
        """The cards' own counts on 24 November: no media errors or predicted failures anywhere;
        other errors on the disks in no array of the R330 and the R430s, none on the T620."""
        other = {"perc-h330-adapter": {0: 8, 1: 8, 2: 8, 3: 8},
                 "perc-h330-mini": {0: 0, 1: 0, 2: 20, 3: 18, 4: 29, 5: 29, 6: 29, 7: 31},
                 "perc-h730p-mini": {0: 0, 1: 0, 2: 24, 3: 28, 4: 35, 5: 35, 6: 35, 7: 33},
                 "perc-h710p-adapter": {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 14: 0}}
        for name, want in other.items():
            with self.subTest(card=name):
                c = read(answers(name), battery=False)[0]
                self.assertEqual({d.slot: d.other_errors for d in c.disks.values()}, want)
                self.assertEqual({d.media_errors for d in c.disks.values()}, {0})
                self.assertEqual({d.predictive_failures for d in c.disks.values()}, {0})

    def test_each_disks_checks_and_temperature(self):
        """As recorded on 24 November: only the T620's bay 5 foreign; nothing else set."""
        temps = {"perc-h330-adapter": {0: 37, 1: 40, 2: 41, 3: 38},
                 "perc-h330-mini": {0: 47, 1: 46, 2: 41, 3: 45, 4: 40, 5: 41, 6: 40, 7: 41},
                 "perc-h730p-mini": {0: 35, 1: 35, 2: 44, 3: 49, 4: 37, 5: 41, 6: 37, 7: 35},
                 "perc-h710p-adapter": {0: 28, 1: 28, 2: 28, 3: 30, 4: 30, 5: 31, 14: 37}}
        for name, want in temps.items():
            with self.subTest(card=name):
                c = read(answers(name), battery=False)[0]
                self.assertEqual({d.slot: d.temperature_celsius for d in c.disks.values()}, want)
                foreign = {d.slot for d in c.disks.values() if d.foreign}
                self.assertEqual(foreign, {5} if name == "perc-h710p-adapter" else set())
                for d in c.disks.values():
                    self.assertFalse(d.bad_block_table_full or d.path_broken or d.unusable or d.disabled_for_removal)

    def test_sizes_are_the_cards_own(self):
        c, _ = read(answers("perc-h330-mini"))
        self.assertEqual(c.arrays[0].size_bytes, 2343043072 * 512)
        self.assertEqual(c.disks[4].size_bytes, 3515088896 * 512)

    def test_4k_disks(self):
        """The R330's ST6000NM0014s have 4096-byte blocks: their size is in those blocks."""
        c, _ = read(answers("perc-h330-adapter"))
        self.assertEqual([c.disks[d].block_size for d in (0, 1, 2, 3)], [512, 4096, 4096, 4096])
        self.assertEqual(c.disks[1].size_bytes, 1464991744 * 4096)
        self.assertEqual(c.disks[0].size_bytes, 11719933952 * 512)

    def test_the_h710p_leaves_the_block_size_0(self):
        c, _ = read(answers("perc-h710p-adapter"))
        self.assertEqual(c.disks[0].block_size, 512)
        self.assertEqual(c.disks[0].size_bytes, 584843264 * 512)

    def test_the_t620s_offline_raid6_and_its_empty_slots(self):
        c, _ = read(answers("perc-h710p-adapter"))
        raid6 = c.arrays[1]
        self.assertEqual((raid6.raid_type, raid6.state_name), ("RAID6", "offline"))
        self.assertEqual([m.device_id for m in raid6.members], [2, 3, 4] + [None] * 9)
        self.assertEqual([m.slot for m in raid6.members if m.device_id is None], list(range(5, 14)))
        self.assertEqual(sorted(c.disks), [0, 1, 2, 3, 4, 5, 14])    # no disk for an empty slot

    def test_the_battery_and_chip(self):
        c, _ = read(answers("perc-h710p-adapter"))
        f = load("perc-h710p-adapter")["fan_control"]
        self.assertEqual(c.chip_celsius, f["chip_celsius"])
        self.assertEqual(c.battery.temperature_celsius, f["battery_celsius"])
        self.assertEqual((c.battery.temp_high, c.battery.charging), (False, False))
        self.assertEqual(c.battery.voltage_mv, 3843)
        c, _ = read(answers("perc-h730p-mini"))
        self.assertTrue(c.battery_present)
        self.assertIsNotNone(c.battery)

    def test_the_fan_controllers_arrays(self):
        c, sent = read(answers("perc-h710p-adapter"), sizes=False, disks=False)
        f = load("perc-h710p-adapter")["fan_control"]
        self.assertEqual(sent, ["CTRL_GET_INFO", "CFG_READ", "CFG_READ", "BBU_GET_STATUS"])
        self.assertEqual({str(t): int(a.write_back_now) for t, a in c.arrays.items() if a.write_back_set},
                         f["vd_write_back"])
        self.assertEqual({str(t): int(a.state == 3) for t, a in c.arrays.items()}, f["vd_optimal"])
        self.assertIsNone(c.disks)

    def test_the_monitors_commands(self):
        _, sent = read(answers("perc-h710p-adapter"), battery=False)
        self.assertEqual(sent, ["CTRL_GET_INFO", "LD_GET_LIST", "CFG_READ", "CFG_READ", "PD_LIST_QUERY"]
                         + ["PD_GET_INFO"] * 7)

    def test_no_battery_read_without_a_battery(self):
        for name in ("perc-h330-mini", "perc-h330-adapter"):
            with self.subTest(card=name):
                c, sent = read(answers(name))
                self.assertFalse(c.battery_present)
                self.assertIsNone(c.battery)
                self.assertNotIn("BBU_GET_STATUS", sent)


def ld_state(a, target, state):
    """Sets an array's state in both LD_GET_LIST and CFG_READ."""
    ll = a["ld_list"]
    for i in range(struct.unpack_from("<I", ll, 0)[0]):
        if ll[8 + 16 * i] == target:
            ll[8 + 16 * i + 4] = state
    cfg = a["cfg_read"]
    ac, asz, lc, lsz = struct.unpack_from("<HHHH", cfg, 4)
    for i in range(lc):
        o = 32 + ac * asz + i * lsz
        if cfg[o] == target:
            cfg[o + 0x26] = state


class Changed(unittest.TestCase):
    def test_a_degraded_raid1_with_a_missing_member(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<HHH", a["cfg_read"], 32 + 0x20 + 8, 0xFFFF, 0, 0)   # array 0, member 1
        ld_state(a, 0, 2)
        c, _ = read(a)
        self.assertEqual(c.arrays[0].state_name, "degraded")
        m = c.arrays[0].members[1]
        self.assertEqual((m.device_id, m.enclosure, m.slot), (None, 32, 1))
        self.assertIsNone(c.disks[1].array)            # the disk itself, now in no array

    def test_a_failed_disk(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<H", a["pd_info/1"], OFF_PD_FW_STATE, 0x11)
        c, _ = read(a)
        self.assertEqual(c.disks[1].state_name, "failed")

    def test_a_rebuild_at_42_percent(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<H", a["pd_info/1"], OFF_PD_FW_STATE, 0x14)
        struct.pack_into("<I", a["pd_info/1"], OFF_PD_PROG_ACTIVE, 1)
        struct.pack_into("<H", a["pd_info/1"], OFF_PD_PROG_REBUILD, 27525)    # 42.0009%
        c, _ = read(a)
        self.assertEqual((c.disks[1].state_name, c.disks[1].rebuild_percent), ("rebuilding", 42))
        self.assertIsNone(c.disks[0].rebuild_percent)

    def test_media_errors_and_a_predicted_failure(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<I", a["pd_info/1"], OFF_PD_MEDIA_ERR, 3)
        struct.pack_into("<I", a["pd_info/1"], OFF_PD_OTHER_ERR, 70000)
        struct.pack_into("<I", a["pd_info/1"], OFF_PD_PRED_FAIL, 1)
        c, _ = read(a)
        d = c.disks[1]
        self.assertEqual((d.media_errors, d.other_errors, d.predictive_failures), (3, 70000, 1))
        self.assertEqual(d.state_name, "online")        # the counts don't change the state

    def test_each_check_set(self):
        for off, value, field in ((OFF_PD_BAD_BLOCK_TABLE_FULL, 1, "bad_block_table_full"),
                                  (OFF_PD_PATH_BROKEN, 0x01, "path_broken"),
                                  (OFF_PD_UNUSABLE, 1, "unusable"),
                                  (OFF_PD_DISABLED_FOR_REMOVAL, 1, "disabled_for_removal")):
            with self.subTest(field=field):
                a = answers("perc-h330-mini")
                a["pd_info/1"][off] |= value
                c, _ = read(a)
                self.assertTrue(getattr(c.disks[1], field))
                self.assertFalse(getattr(c.disks[0], field))
        a = answers("perc-h330-mini")
        struct.pack_into("<H", a["pd_info/1"], OFF_PD_DDF_TYPE, struct.unpack_from("<H", a["pd_info/1"], OFF_PD_DDF_TYPE)[0] | 0x10)
        a["pd_info/1"][OFF_PD_PATH_BROKEN] = 0x80                  # widePortCapable alone: not broken
        c, _ = read(a)
        self.assertTrue(c.disks[1].foreign)
        self.assertFalse(c.disks[1].path_broken)

    def test_a_disk_temperature_of_0_or_255_is_no_reading(self):
        for raw in (0, 0xFF):
            a = answers("perc-h330-mini")
            a["pd_info/1"][OFF_PD_TEMPERATURE] = raw
            c, _ = read(a)
            self.assertIsNone(c.disks[1].temperature_celsius)

    def test_an_unknown_state_is_shown_by_number(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<H", a["pd_info/1"], OFF_PD_FW_STATE, 0x99)
        ld_state(a, 0, 7)
        c, _ = read(a)
        self.assertEqual((c.disks[1].state_name, c.arrays[0].state_name), ("state 0x99", "state 7"))

    def test_the_battery_charging_and_too_hot(self):
        a = answers("perc-h710p-adapter")
        struct.pack_into("<I", a["bbu_status"], 8, 0x08)
        c, _ = read(a, disks=False)
        self.assertEqual((c.battery.charging, c.battery.temp_high), (True, False))
        struct.pack_into("<I", a["bbu_status"], 8, 0x04)
        c, _ = read(a, disks=False)
        self.assertEqual((c.battery.charging, c.battery.temp_high), (False, True))

    def test_a_chip_of_0_or_255_is_no_reading(self):
        for roc in (0, 255):
            with self.subTest(roc=roc):
                a = answers("perc-h710p-adapter")
                a["ctrl_info"][OFF_TEMPERATURE_ROC] = roc
                c, _ = read(a, disks=False)
                self.assertIsNone(c.chip_celsius)

    def test_write_through_now(self):
        a = answers("perc-h710p-adapter")
        cfg = a["cfg_read"]
        ac, asz, lc, lsz = struct.unpack_from("<HHHH", cfg, 4)
        cfg[32 + ac * asz + 0x17] &= ~1                 # the first array's cache now
        c, _ = read(a, disks=False)
        first = cfg[32 + ac * asz]
        self.assertEqual((c.arrays[first].write_back_set, c.arrays[first].write_back_now), (True, False))


class Failures(unittest.TestCase):
    """Any of these is a failed read: PercError, and nothing returned."""

    def assertFails(self, a, **kw):
        with self.assertRaises(PercError):
            read(a, **kw)

    def test_each_firmware_status_other_than_ok(self):
        for key in ("ctrl_info", "ld_list", "cfg_read_header", "cfg_read", "pd_list", "pd_info/3",
                    "bbu_status"):
            with self.subTest(answer=key):
                self.assertFails(answers("perc-h730p-mini"), status={key: 0x03})

    def test_not_a_percs_answer(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<H", a["ctrl_info"], 0, 0x8086)
        self.assertFails(a)
        a = answers("perc-h330-mini")
        a["ctrl_info"][0x540:0x590] = bytes(80)
        self.assertFails(a)

    def test_short_answers(self):
        a = answers("perc-h730p-mini")
        for parse, key in ((parse_ctrl_info, "ctrl_info"), (parse_pd_info, "pd_info/0"),
                           (parse_bbu_status, "bbu_status"), (parse_ld_list, "ld_list"),
                           (parse_pd_list, "pd_list"), (parse_config, "cfg_read")):
            with self.subTest(answer=key), self.assertRaises(PercError):
                parse(bytes(a[key][:6]))

    def test_cfg_size_out_of_range(self):
        for size in (16, 65537, 1 << 31):
            with self.subTest(size=size):
                a = answers("perc-h710p-adapter")
                struct.pack_into("<I", a["cfg_read_header"], 0, size)
                self.assertFails(a)

    def test_counts_past_their_buffers(self):
        a = answers("perc-h710p-adapter")
        struct.pack_into("<I", a["ld_list"], 0, 1000)
        self.assertFails(a)
        a = answers("perc-h710p-adapter")
        struct.pack_into("<I", a["pd_list"], 4, 400)
        self.assertFails(a)
        a = answers("perc-h710p-adapter")
        struct.pack_into("<H", a["cfg_read"], 4, 50)        # 50 arrays in 1664 bytes
        self.assertFails(a)
        a = answers("perc-h710p-adapter")
        a["cfg_read"][32 + 8] = 40                           # 40 members in a 288-byte array
        self.assertFails(a)

    def test_a_member_missing_from_the_device_list(self):
        """A disk pulled mid-read: the next read is clean."""
        a = answers("perc-h330-mini")
        pl = a["pd_list"]
        for i in range(struct.unpack_from("<I", pl, 4)[0]):
            if struct.unpack_from("<H", pl, 8 + 24 * i)[0] == 1:
                pl[8 + 24 * i + 6] = 0x0D                    # no longer a disk
        self.assertFails(a)

    def test_the_array_list_and_configuration_disagree(self):
        a = answers("perc-h330-mini")
        struct.pack_into("<I", a["ld_list"], 0, 0)
        self.assertFails(a)


if __name__ == "__main__":
    unittest.main()
