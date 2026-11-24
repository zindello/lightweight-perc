"""Talking to the card: read-direction DCMD frames through the megaraid_sas driver's management
ioctl, MEGASAS_IOC_FIRMWARE on /dev/megaraid_sas_ioctl_node. That is how Dell's perccli talks to
the card too.

Only the six firmware commands in READS are ever sent, each with its fixed answer size and
mailbox. Anything else raises ReadOnlyViolation before the device is opened. Every frame is built
with the read direction (MFI_FRAME_DIR_READ) and one buffer the card fills.

The layouts are the Linux kernel's, drivers/scsi/megaraid/megaraid_sas.h (v6.12):
  struct megasas_iocpacket   host_no u16, pad u16, sgl_off u32, sge_count u32, sense_off u32,
                             sense_len u32, frame raw[128], struct iovec sgl[MAX_IOCTL_SGE=16];
                             packed: 20 + 128 + 16 * 16 = 404 bytes
  MEGASAS_IOC_FIRMWARE       _IOWR('M', 1, struct megasas_iocpacket)
  struct megasas_dcmd_frame  cmd u8 @0x00, cmd_status u8 @0x02, sge_count u8 @0x07,
                             flags le16 @0x10, timeout le16 @0x12, data_xfer_len le32 @0x14,
                             opcode le32 @0x18, mbox[12] @0x1C, sgl @0x28
The driver (megaraid_sas_base.c, megasas_mgmt_fw_ioctl) copies the frame, points the SGE at
sgl_off to a kernel buffer the size of sgl[0], issues it, copies the buffer back and writes the
firmware's status into frame.hdr.cmd_status.
"""

import ctypes
import fcntl
import logging
import os
import stat
import struct

log = logging.getLogger("lightweight_perc")

NODE = "/dev/megaraid_sas_ioctl_node"
SYS_SCSI_HOST = "/sys/class/scsi_host"
PROC_DEVICES = "/proc/devices"

MFI_CMD_DCMD = 0x05
MFI_FRAME_DIR_READ = 0x0010
MFI_STAT_OK = 0x00
STATUS_UNSET = 0xFF
SGL_OFF = 0x28
MAX_IOCTL_SGE = 16
FRAME_OFF = 20                      # the frame's offset in struct megasas_iocpacket
IOCPACKET_SIZE = FRAME_OFF + 128 + 16 * MAX_IOCTL_SGE
MEGASAS_IOC_FIRMWARE = (3 << 30) | (IOCPACKET_SIZE << 16) | (ord("M") << 8) | 1   # _IOWR('M', 1, ...)

# The six read-only DCMDs (Linux MR_DCMD_*, FreeBSD MFI_DCMD_*).
CTRL_GET_INFO = 0x01010000    # Linux megasas_get_ctrl_info();  FreeBSD mfi_ctrl_get_info()
PD_LIST_QUERY = 0x02010100    # Linux megasas_get_pd_list();    mbox[0] = MR_PD_QUERY_TYPE_ALL (0)
PD_GET_INFO = 0x02020000      # Linux megasas_get_pd_info();    mbox: the device id, le16
LD_GET_LIST = 0x03010000      # Linux megasas_get_ld_list();    FreeBSD mfi_ld_get_list()
CFG_READ = 0x04010000         # FreeBSD mfi_config_read() (mfiutil)
BBU_GET_STATUS = 0x05010000   # FreeBSD mfiutil "show battery"

CTRL_INFO_SIZE = 2384         # struct megasas_ctrl_info
PD_LIST_SIZE = 8192           # struct MR_PD_LIST: 8 + 24 per device, room for 340
PD_INFO_SIZE = 512            # struct MR_PD_INFO
LD_LIST_SIZE = 8 + 16 * 256   # struct MR_LD_LIST, MAX_LOGICAL_DRIVES_EXT entries
CFG_HEADER_SIZE = 32          # struct mfi_config_data, its header
CFG_MAX_SIZE = 65536
BBU_STATUS_SIZE = 64          # struct mfi_bbu_status

# opcode -> (name, the answer sizes allowed, the mailbox sizes allowed)
READS = {
    CTRL_GET_INFO: ("CTRL_GET_INFO", (CTRL_INFO_SIZE, CTRL_INFO_SIZE), (0,)),
    PD_LIST_QUERY: ("PD_LIST_QUERY", (PD_LIST_SIZE, PD_LIST_SIZE), (1,)),
    PD_GET_INFO: ("PD_GET_INFO", (PD_INFO_SIZE, PD_INFO_SIZE), (2,)),
    LD_GET_LIST: ("LD_GET_LIST", (LD_LIST_SIZE, LD_LIST_SIZE), (0,)),
    CFG_READ: ("CFG_READ", (CFG_HEADER_SIZE, CFG_MAX_SIZE), (0,)),
    BBU_GET_STATUS: ("BBU_GET_STATUS", (BBU_STATUS_SIZE, BBU_STATUS_SIZE), (0,)),
}


class PercError(RuntimeError):
    """A read that didn't work. Nothing read in it is returned."""


class ReadOnlyViolation(PercError):
    """A frame that isn't one of the six reads: refused before anything is sent."""


def check_read(opcode, length, mbox=b""):
    """Raises ReadOnlyViolation unless this is one of READS, with its own sizes."""
    if opcode not in READS:
        raise ReadOnlyViolation(f"not one of the read-only commands: {opcode:#010x}")
    name, (low, high), mbox_sizes = READS[opcode]
    if not low <= length <= high:
        raise ReadOnlyViolation(f"{name}: answer size {length} outside {low}-{high}")
    if len(mbox) not in mbox_sizes:
        raise ReadOnlyViolation(f"{name}: mailbox of {len(mbox)} bytes")
    if opcode == PD_LIST_QUERY and mbox != b"\x00":
        raise ReadOnlyViolation("PD_LIST_QUERY: only MR_PD_QUERY_TYPE_ALL")
    return name


def packet(host_no, opcode, buf_addr, length, mbox=b""):
    """The ioctl packet for one read: a read-direction DCMD frame with one SGE."""
    check_read(opcode, length, mbox)
    frame = bytearray(128)
    frame[0x00] = MFI_CMD_DCMD
    frame[0x02] = STATUS_UNSET
    frame[0x07] = 1                                            # sge_count
    struct.pack_into("<HHII", frame, 0x10, MFI_FRAME_DIR_READ, 0, length, opcode)
    frame[0x1C:0x1C + len(mbox)] = mbox
    sgl = struct.pack("<QQ", buf_addr, length) + bytes(16 * (MAX_IOCTL_SGE - 1))
    pkt = bytearray(struct.pack("<HHIIII", host_no, 0, SGL_OFF, 1, 0, 0) + frame + sgl)
    # Checked again on the bytes themselves: one DCMD, read direction, one of the six.
    if (len(pkt) != IOCPACKET_SIZE or pkt[FRAME_OFF] != MFI_CMD_DCMD
            or struct.unpack_from("<H", pkt, FRAME_OFF + 0x10)[0] != MFI_FRAME_DIR_READ
            or struct.unpack_from("<I", pkt, FRAME_OFF + 0x18)[0] not in READS):
        raise ReadOnlyViolation("frame check failed")
    return pkt


def megaraid_host(sys_scsi_host=SYS_SCSI_HOST):
    """The SCSI host number the megaraid_sas driver registered (the ioctl names its card by it)."""
    hosts = []
    for name in sorted(os.listdir(sys_scsi_host)):
        try:
            with open(os.path.join(sys_scsi_host, name, "proc_name")) as f:
                if f.read().strip() == "megaraid_sas" and name.startswith("host"):
                    hosts.append(int(name[4:]))
        except OSError:
            continue
    if len(hosts) != 1:
        raise PercError(f"expected one megaraid_sas host, found {hosts}")
    return hosts[0]


def ensure_megaraid_node(path=NODE, proc_devices=PROC_DEVICES):
    """Makes the ioctl device node if it isn't there. Some systems (Talos, for one) load
    megaraid_sas, which registers megaraid_sas_ioctl in /proc/devices, but nothing makes the node.
    It's made as perccli itself would: a character device on the driver's major, minor 0. Needs
    root (or CAP_MKNOD) in a container that may make device nodes."""
    if os.path.exists(path):
        return
    major = None
    with open(proc_devices) as f:
        for line in f:
            fields = line.split()
            if len(fields) == 2 and fields[1] == "megaraid_sas_ioctl":
                major = int(fields[0])
    if major is None:
        raise PercError("megaraid_sas_ioctl isn't in /proc/devices: no megaraid_sas driver")
    os.mknod(path, 0o600 | stat.S_IFCHR, os.makedev(major, 0))
    log.info("made %s (character device %d, 0)", path, major)


class Card:
    """One card, through the driver's ioctl. The device is opened at the first read and closed by
    close() (or the with block). dcmd() sends only the six reads."""

    def __init__(self, node=NODE, sys_scsi_host=SYS_SCSI_HOST, ioctl=fcntl.ioctl):
        self.node = node
        self.sys_scsi_host = sys_scsi_host
        self.ioctl = ioctl
        self.host_no = None
        self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def dcmd(self, opcode, length, mbox=b""):
        """One read: returns the card's answer, or raises PercError if the firmware's status
        isn't OK."""
        name = check_read(opcode, length, mbox)       # before the device is ever opened
        if self.host_no is None:
            self.host_no = megaraid_host(self.sys_scsi_host)
        buf = ctypes.create_string_buffer(length)
        pkt = packet(self.host_no, opcode, ctypes.addressof(buf), length, mbox)
        if self.fd is None:
            self.fd = os.open(self.node, os.O_RDWR)
        try:
            self.ioctl(self.fd, MEGASAS_IOC_FIRMWARE, pkt, True)
        except OSError as e:
            raise PercError(f"{name}: ioctl failed: {e}") from e
        status = pkt[FRAME_OFF + 0x02]
        if status != MFI_STAT_OK:
            raise PercError(f"{name}: firmware status {status:#04x}")
        return bytes(buf.raw)
