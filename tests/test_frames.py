"""What is sent: the exact bytes of every frame, the read direction, and the refusals."""

import ctypes
import os
import stat
import struct
import tempfile
import unittest
from unittest import mock

from lightweight_perc import ioctl
from lightweight_perc.ioctl import (BBU_GET_STATUS, CFG_READ, CTRL_GET_INFO, FRAME_OFF, LD_GET_LIST,
                                    PD_GET_INFO, PD_LIST_QUERY, Card, PercError, ReadOnlyViolation)

ADDR = 0x1122334455667788

# The first 0x28 bytes of each frame (megasas_dcmd_frame up to its SGL), written out by hand:
# cmd 05, status ff (unset), sge_count 1, flags 0010 (read), length, opcode, mailbox.
FRAMES = {
    (CTRL_GET_INFO, 2384, b""):
        "0500ff0000000001" "0000000000000000" "10000000" "50090000" "00000101" + "00" * 12,
    (LD_GET_LIST, 4104, b""):
        "0500ff0000000001" "0000000000000000" "10000000" "08100000" "00000103" + "00" * 12,
    (CFG_READ, 32, b""):
        "0500ff0000000001" "0000000000000000" "10000000" "20000000" "00000104" + "00" * 12,
    (CFG_READ, 1664, b""):
        "0500ff0000000001" "0000000000000000" "10000000" "80060000" "00000104" + "00" * 12,
    (PD_LIST_QUERY, 8192, b"\x00"):
        "0500ff0000000001" "0000000000000000" "10000000" "00200000" "00010102" + "00" * 12,
    (PD_GET_INFO, 512, b"\x0e\x00"):
        "0500ff0000000001" "0000000000000000" "10000000" "00020000" "00000202" "0e00" + "00" * 10,
    (BBU_GET_STATUS, 64, b""):
        "0500ff0000000001" "0000000000000000" "10000000" "40000000" "00000105" + "00" * 12,
}


class Frames(unittest.TestCase):
    def test_the_kernels_layout(self):
        # megaraid_sas.h: struct megasas_iocpacket is 404 bytes packed; _IOWR('M', 1, it).
        self.assertEqual(ioctl.IOCPACKET_SIZE, 404)
        self.assertEqual(ioctl.MEGASAS_IOC_FIRMWARE, 0xC1944D01)

    def test_every_frame_exactly(self):
        for (opcode, length, mbox), head in FRAMES.items():
            with self.subTest(opcode=hex(opcode), length=length):
                pk = ioctl.packet(3, opcode, ADDR, length, mbox)
                expected = (struct.pack("<HHIIII", 3, 0, 0x28, 1, 0, 0)
                            + bytes.fromhex(head) + bytes(128 - 0x28)
                            + struct.pack("<QQ", ADDR, length) + bytes(16 * 15))
                self.assertEqual(bytes(pk), expected)
                self.assertEqual(struct.unpack_from("<H", pk, FRAME_OFF + 0x10)[0], 0x0010)

    def test_only_six_opcodes(self):
        self.assertEqual(sorted(ioctl.READS), sorted({o for o, _, _ in FRAMES}))

    def test_refused(self):
        for opcode, length, mbox in [
            (0x01020000, 2384, b""),        # not one of the six (a controller property read)
            (0x05030000, 64, b""),          # not one of the six (a battery command)
            (0x00000000, 64, b""),
            (CTRL_GET_INFO, 4096, b""),     # the wrong size
            (CFG_READ, 65537, b""),
            (CFG_READ, 16, b""),
            (PD_GET_INFO, 512, b""),        # no device id
            (CTRL_GET_INFO, 2384, b"\x01"), # a mailbox where there is none
            (PD_LIST_QUERY, 8192, b"\x01"), # any query but "all devices"
        ]:
            with self.subTest(opcode=hex(opcode), length=length, mbox=mbox):
                with self.assertRaises(ReadOnlyViolation):
                    ioctl.packet(0, opcode, ADDR, length, mbox)

    def test_a_refused_command_never_reaches_the_device(self):
        sent = []
        card = Card(node="/nonexistent/megaraid_sas_ioctl_node", sys_scsi_host="/nonexistent",
                    ioctl=lambda *a: sent.append(a))
        with mock.patch("os.open") as opened:
            with self.assertRaises(ReadOnlyViolation):
                card.dcmd(0x01020000, 2384)
            opened.assert_not_called()
        self.assertEqual(sent, [])


def scsi_hosts(root, **hosts):
    for name, proc in hosts.items():
        os.makedirs(os.path.join(root, name), exist_ok=True)
        with open(os.path.join(root, name, "proc_name"), "w") as f:
            f.write(proc + "\n")
    return root


class TheCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.node = os.path.join(self.root, "node")
        open(self.node, "w").close()

    def card(self, status=0, fill=b"\x5a"):
        calls = []

        def fake_ioctl(fd, request, pkt, mutate):
            calls.append((request, bytes(pkt)))
            addr, length = struct.unpack_from("<QQ", pkt, FRAME_OFF + 128)
            ctypes.memmove(addr, fill * length, length)
            pkt[FRAME_OFF + 2] = status
        hosts = scsi_hosts(os.path.join(self.root, "sys"), host0="ahci", host2="megaraid_sas")
        return Card(self.node, hosts, ioctl=fake_ioctl), calls

    def test_a_read(self):
        card, calls = self.card()
        with card:
            self.assertEqual(card.dcmd(BBU_GET_STATUS, 64), b"\x5a" * 64)
        self.assertEqual(len(calls), 1)
        request, pkt = calls[0]
        self.assertEqual(request, ioctl.MEGASAS_IOC_FIRMWARE)
        self.assertEqual(struct.unpack_from("<H", pkt, 0)[0], 2)          # host2
        self.assertIsNone(card.fd)                                       # closed

    def test_a_firmware_error_raises(self):
        for status in (0x01, 0x03, 0x0C, 0xFF):
            with self.subTest(status=status):
                card, _ = self.card(status=status)
                with card, self.assertRaises(PercError):
                    card.dcmd(CTRL_GET_INFO, 2384)

    def test_ioctl_failure_raises(self):
        def boom(*a):
            raise OSError(5, "Input/output error")
        hosts = scsi_hosts(os.path.join(self.root, "sys"), host0="megaraid_sas")
        with Card(self.node, hosts, ioctl=boom) as card, self.assertRaises(PercError):
            card.dcmd(CTRL_GET_INFO, 2384)

    def test_megaraid_host_none_or_many(self):
        for i, hosts in enumerate([{"host1": "ahci"},
                                   {"host0": "megaraid_sas", "host3": "megaraid_sas"}]):
            with self.subTest(hosts=hosts), self.assertRaises(PercError):
                ioctl.megaraid_host(scsi_hosts(os.path.join(self.root, f"s{i}"), **hosts))


class TheNode(unittest.TestCase):
    def test_made_from_proc_devices(self):
        with tempfile.TemporaryDirectory() as d:
            proc = os.path.join(d, "devices")
            with open(proc, "w") as f:
                f.write("Character devices:\n  1 mem\n239 megaraid_sas_ioctl\n245 ipmidev\n")
            made = []
            with mock.patch("os.mknod", lambda p, m, dev: made.append((p, m, os.major(dev), os.minor(dev)))):
                ioctl.ensure_megaraid_node(os.path.join(d, "node"), proc)
            self.assertEqual(made, [(os.path.join(d, "node"), 0o600 | stat.S_IFCHR, 239, 0)])

    def test_left_alone_when_there(self):
        with tempfile.NamedTemporaryFile() as f, mock.patch("os.mknod") as mknod:
            ioctl.ensure_megaraid_node(f.name, "/nonexistent")
            mknod.assert_not_called()

    def test_no_driver(self):
        with tempfile.TemporaryDirectory() as d:
            proc = os.path.join(d, "devices")
            with open(proc, "w") as f:
                f.write("Character devices:\n  1 mem\n")
            with self.assertRaises(PercError):
                ioctl.ensure_megaraid_node(os.path.join(d, "node"), proc)


if __name__ == "__main__":
    unittest.main()
