"""The card's answers, parsed. Pure functions of bytes: no device here.

Layouts: Linux v6.12 drivers/scsi/megaraid/megaraid_sas.h (megasas_ctrl_info, MR_LD_LIST,
MR_PD_LIST, MR_PD_INFO) and FreeBSD releng/14.1 sys/dev/mfi/mfireg.h (mfi_config_data, mfi_array,
mfi_ld_config, mfi_bbu_status), for the commands the Linux driver never sends itself. The two
agree wherever they overlap. Every offset below was checked with offsetof() against the headers.

Anything that doesn't add up raises PercError: a count past its buffer, a size under its header,
an answer that isn't a PERC's.
"""

import struct
from dataclasses import dataclass

from .ioctl import CFG_HEADER_SIZE, CFG_MAX_SIZE, CTRL_INFO_SIZE, PercError

LSI_VENDOR_ID = 0x1000

# megasas_ctrl_info
OFF_VENDOR_ID = 0x000
OFF_PRODUCT_NAME = 0x540        # char product_name[80]
OFF_HW_PRESENT = 0x5B0          # u32 bitfield; bit 0 bbu
OFF_TEMPERATURE_ROC = 0x7C9     # u8, °C; 0 and 0xFF mean not reported

# MR_PD_INFO
OFF_PD_INQUIRY = 0x04           # u8 inquiryData[96]: vendor @8, product @16, revision @32
OFF_PD_MEDIA_ERR = 0xA8         # u32 mediaErrCount
OFF_PD_OTHER_ERR = 0xAC         # u32 otherErrCount
OFF_PD_PRED_FAIL = 0xB0         # u32 predFailCount
OFF_PD_FW_STATE = 0xB8          # u16
OFF_PD_DISABLED_FOR_REMOVAL = 0xBA  # u8
OFF_PD_DDF_TYPE = 0xBC          # u16 MR_PD_DDF_TYPE.ddf.pdType; bit 4 isForeign
OFF_PD_PATH_BROKEN = 0xC1       # u8 pathInfo: isPathBroken, the low 4 bits
OFF_PD_COERCED = 0xF8           # u64, in the disk's blocks
OFF_PD_ENCL = 0x100             # u16 enclDeviceId
OFF_PD_SLOT = 0x103             # u8 slotNumber
OFF_PD_PROG_ACTIVE = 0x104      # MR_PD_PROGRESS: u32 active, bit 0 rebuild
OFF_PD_PROG_REBUILD = 0x108     # u16 progress, 0-65535
OFF_PD_BAD_BLOCK_TABLE_FULL = 0x124  # u8, after MR_PD_PROGRESS (32 bytes)
OFF_PD_UNUSABLE = 0x125         # u8 unusableInCurrentConfig
OFF_PD_TEMPERATURE = 0x192      # u8, the disk's own °C; 0 and 0xFF mean not reported
OFF_PD_BLOCK_SIZE = 0x194       # u16 userDataBlockSize (some cards leave it 0: 512)

NO_DEVICE = 0xFFFF              # mfi_array: "0xffff == missing drive"

ARRAY_STATES = {0: "offline", 1: "partially degraded", 2: "degraded", 3: "optimal"}
OPTIMAL = 3
DISK_STATES = {0x00: "unconfigured", 0x01: "bad", 0x02: "hot spare", 0x10: "offline",
               0x11: "failed", 0x14: "rebuilding", 0x18: "online", 0x20: "copying back",
               0x40: "jbod"}
REBUILDING = 0x14
RAID_LEVELS = {0: "RAID0", 1: "RAID1", 5: "RAID5", 6: "RAID6"}


def cstr(b):
    return bytes(b).split(b"\0")[0].decode("ascii", "replace").strip()


def array_state_name(state):
    return ARRAY_STATES.get(state, f"state {state}")


def disk_state_name(fw_state):
    return DISK_STATES.get(fw_state, f"state {fw_state:#04x}")


def raid_type(primary_level, span_depth):
    """perccli's TYPE: RAID0, 1, 5 or 6, with a 0 added when it spans more than one array (RAID10,
    50, 60)."""
    base = RAID_LEVELS.get(primary_level, f"level {primary_level}")
    if span_depth > 1 and primary_level in (1, 5, 6):
        base += "0"
    return base


@dataclass(frozen=True)
class ControllerInfo:
    vendor_id: int
    model: str                  # product_name, trimmed ("PERC H730P Mini")
    battery_present: bool       # hw_present.bbu
    chip_celsius: int | None    # temperatureROC; None when the card gives 0 or 255


@dataclass(frozen=True)
class Member:
    device_id: int | None       # None: no disk there (mfi_array's 0xffff)
    enclosure: int
    slot: int
    fw_state: int


@dataclass(frozen=True)
class Array:
    target_id: int              # perccli's VD number
    name: str
    raid_type: str
    state: int                  # 3 optimal, 2 degraded, 1 partially degraded, 0 offline
    write_back_set: bool        # the cache policy as set
    write_back_now: bool        # the cache policy in force now
    members: tuple[Member, ...]
    size_bytes: int | None = None   # from LD_GET_LIST, when it's read

    @property
    def state_name(self):
        return array_state_name(self.state)


@dataclass(frozen=True)
class PdAddress:
    device_id: int
    enclosure: int
    slot: int
    scsi_type: int              # 0: a disk


@dataclass(frozen=True)
class Disk:
    device_id: int
    enclosure: int
    slot: int
    vendor: str
    model: str                  # the inquiry data's product field, trimmed
    fw_state: int
    block_size: int
    size_bytes: int             # coerced size x block size
    rebuild_percent: int | None # while a rebuild is active, rounded down
    array: int | None = None    # the target id of the array it's a member of, if any
    media_errors: int = 0       # the controller's counts for the disk (mediaErrCount,
    other_errors: int = 0       # otherErrCount, predFailCount): kept by the controller, since
    predictive_failures: int = 0  # the disk was fitted
    foreign: bool = False       # it carries a configuration the controller hasn't imported
    bad_block_table_full: bool = False   # no room left to remap bad blocks
    path_broken: bool = False   # a SAS path to it is down (pathInfo.isPathBroken)
    unusable: bool = False      # unusable in the current configuration
    disabled_for_removal: bool = False   # prepared for removal
    temperature_celsius: int | None = None   # the disk's own; None when not reported

    @property
    def state_name(self):
        return disk_state_name(self.fw_state)


@dataclass(frozen=True)
class BatteryStatus:
    temperature_celsius: int
    voltage_mv: int
    fw_status: int
    temp_high: bool             # fw_status bit 2
    charging: bool              # fw_status bit 3


def _need(b, n, what):
    if len(b) < n:
        raise PercError(f"{what}: short answer, {len(b)} bytes")


def parse_ctrl_info(b):
    _need(b, CTRL_INFO_SIZE, "CTRL_GET_INFO")
    vendor = struct.unpack_from("<H", b, OFF_VENDOR_ID)[0]
    model = cstr(b[OFF_PRODUCT_NAME:OFF_PRODUCT_NAME + 80])
    if vendor != LSI_VENDOR_ID or not model:
        raise PercError(f"CTRL_GET_INFO: not a PERC's answer (vendor {vendor:#06x}, product {model!r})")
    roc = b[OFF_TEMPERATURE_ROC]
    return ControllerInfo(vendor_id=vendor, model=model,
                          battery_present=bool(struct.unpack_from("<I", b, OFF_HW_PRESENT)[0] & 1),
                          chip_celsius=None if roc in (0, 0xFF) else roc)


def parse_ld_list(b):
    """MR_LD_LIST -> {target id: (state, size in bytes)}. Sizes are in 512-byte blocks."""
    _need(b, 8, "LD_GET_LIST")
    count = struct.unpack_from("<I", b, 0)[0]
    if 8 + 16 * count > len(b):
        raise PercError(f"LD_GET_LIST: {count} arrays run past the answer")
    out = {}
    for i in range(count):
        o = 8 + 16 * i
        out[b[o]] = (b[o + 4], struct.unpack_from("<Q", b, o + 8)[0] * 512)
    return out


def cfg_size(header):
    """CFG_READ's whole size, from its header (read first, as FreeBSD's mfiutil does)."""
    _need(header, CFG_HEADER_SIZE, "CFG_READ")
    size = struct.unpack_from("<I", header, 0)[0]
    if not CFG_HEADER_SIZE <= size <= CFG_MAX_SIZE:
        raise PercError(f"CFG_READ: size {size} outside {CFG_HEADER_SIZE}-{CFG_MAX_SIZE}")
    return size


def parse_config(b):
    """mfi_config_data -> {target id: Array}, without sizes."""
    size = cfg_size(b)
    _need(b, size, "CFG_READ")
    array_count, array_size, ld_count, ld_size = struct.unpack_from("<HHHH", b, 4)
    if CFG_HEADER_SIZE + array_count * array_size + ld_count * ld_size > size:
        raise PercError("CFG_READ: its arrays run past the answer")
    if (array_count and array_size < 0x20) or (ld_count and ld_size < 0x40):
        raise PercError("CFG_READ: record sizes too small")
    arrays = {}
    for a in range(array_count):
        o = CFG_HEADER_SIZE + a * array_size
        drives, ref = b[o + 8], struct.unpack_from("<H", b, o + 10)[0]
        if 0x20 + 8 * drives > array_size:
            raise PercError(f"CFG_READ: array {ref}'s {drives} members run past it")
        members = []
        for k in range(drives):
            p = o + 0x20 + 8 * k
            dev, _seq, fw = struct.unpack_from("<HHH", b, p)
            members.append(Member(device_id=None if dev == NO_DEVICE else dev,
                                  enclosure=b[p + 6], slot=b[p + 7], fw_state=fw))
        arrays[ref] = tuple(members)
    out = {}
    lo = CFG_HEADER_SIZE + array_count * array_size
    for i in range(ld_count):
        o = lo + i * ld_size
        target, span_depth = b[o], b[o + 0x25]
        if 0x40 + 24 * span_depth > ld_size:
            raise PercError(f"CFG_READ: array {target}'s {span_depth} spans run past it")
        members = []
        for s in range(span_depth):
            ref = struct.unpack_from("<H", b, o + 0x40 + 24 * s + 16)[0]
            if ref not in arrays:
                raise PercError(f"CFG_READ: array {target} spans array {ref}, which isn't there")
            members.extend(arrays[ref])
        out[target] = Array(
            target_id=target, name=cstr(b[o + 4:o + 20]),
            raid_type=raid_type(b[o + 0x20], span_depth), state=b[o + 0x26],
            write_back_set=bool(b[o + 0x14] & 1), write_back_now=bool(b[o + 0x17] & 1),
            members=tuple(members))
    return out


def parse_pd_list(b):
    """MR_PD_LIST -> [PdAddress], every device the card sees (enclosures too)."""
    _need(b, 8, "PD_LIST_QUERY")
    count = struct.unpack_from("<I", b, 4)[0]
    if 8 + 24 * count > len(b):
        raise PercError(f"PD_LIST_QUERY: {count} devices run past the answer")
    out = []
    for i in range(count):
        o = 8 + 24 * i
        dev, encl, _index, slot, typ = struct.unpack_from("<HHBBB", b, o)
        out.append(PdAddress(device_id=dev, enclosure=encl, slot=slot, scsi_type=typ))
    return out


def parse_pd_info(b):
    _need(b, 512, "PD_GET_INFO")
    inquiry = b[OFF_PD_INQUIRY:OFF_PD_INQUIRY + 96]
    block = struct.unpack_from("<H", b, OFF_PD_BLOCK_SIZE)[0] or 512
    active = struct.unpack_from("<I", b, OFF_PD_PROG_ACTIVE)[0] & 1
    progress = struct.unpack_from("<H", b, OFF_PD_PROG_REBUILD)[0]
    return Disk(device_id=struct.unpack_from("<H", b, 0)[0],
                  enclosure=struct.unpack_from("<H", b, OFF_PD_ENCL)[0], slot=b[OFF_PD_SLOT],
                  vendor=cstr(inquiry[8:16]), model=cstr(inquiry[16:32]),
                  fw_state=struct.unpack_from("<H", b, OFF_PD_FW_STATE)[0], block_size=block,
                  size_bytes=struct.unpack_from("<Q", b, OFF_PD_COERCED)[0] * block,
                  rebuild_percent=progress * 100 // 65535 if active else None,
                  media_errors=struct.unpack_from("<I", b, OFF_PD_MEDIA_ERR)[0],
                  other_errors=struct.unpack_from("<I", b, OFF_PD_OTHER_ERR)[0],
                  predictive_failures=struct.unpack_from("<I", b, OFF_PD_PRED_FAIL)[0],
                  foreign=bool(struct.unpack_from("<H", b, OFF_PD_DDF_TYPE)[0] & 0x10),
                  bad_block_table_full=bool(b[OFF_PD_BAD_BLOCK_TABLE_FULL]),
                  path_broken=bool(b[OFF_PD_PATH_BROKEN] & 0x0F),
                  unusable=bool(b[OFF_PD_UNUSABLE]),
                  disabled_for_removal=bool(b[OFF_PD_DISABLED_FOR_REMOVAL]),
                  temperature_celsius=None if b[OFF_PD_TEMPERATURE] in (0, 0xFF) else b[OFF_PD_TEMPERATURE])


def parse_bbu_status(b):
    _need(b, 64, "BBU_GET_STATUS")
    voltage, temperature, fw_status = (struct.unpack_from("<H", b, 2)[0],
                                       struct.unpack_from("<H", b, 6)[0],
                                       struct.unpack_from("<I", b, 8)[0])
    return BatteryStatus(temperature_celsius=temperature, voltage_mv=voltage, fw_status=fw_status,
                         temp_high=bool(fw_status & 0x04), charging=bool(fw_status & 0x08))
